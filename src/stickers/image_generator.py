"""
Replicate image generator for Sticker Trendz.

Generates sticker images via Replicate (model configurable via REPLICATE_MODEL_ID).
Orchestrates the full flow: prompt generation, image generation, quality
validation, post-processing, and upload to R2.
"""

from __future__ import annotations

import io
import logging
import sys
from typing import Any, Dict, List, Optional

import httpx

from src.config import load_config, setup_logging
from src.db import SupabaseClient, DatabaseError
from src.resilience import retry, RetryExhaustedError
from src.monitoring.pipeline_logger import PipelineRunLogger
from src.monitoring.error_logger import ErrorLogger
from src.monitoring.alerter import EmailAlerter
from src.monitoring.spend_tracker import SpendTracker, estimate_replicate_cost
from src.publisher.storage import R2StorageClient
from src.stickers.prompt_generator import PromptGenerator
from src.stickers.quality_validator import validate_image, get_modified_prompt
from src.stickers.post_processor import process_image, PostProcessingError

logger = logging.getLogger(__name__)

IMAGES_PER_TREND = 3
MAX_QUALITY_RETRIES = 2
WORKFLOW_NAME = "sticker_generator"


class ImageGeneratorError(Exception):
    """Raised on image generation failures."""


class ImageGenerator:
    """
    Replicate image generator with full pipeline orchestration.

    For each trend with status='discovered':
      1. Generate 3 image prompts (via PromptGenerator)
      2. Generate images via Replicate (model from REPLICATE_MODEL_ID)
      3. Validate quality
      4. Post-process (crop, resize, transparency)
      5. Upload to R2
      6. Create sticker records in Supabase
    """

    def __init__(
        self,
        db: Optional[SupabaseClient] = None,
        prompt_generator: Optional[PromptGenerator] = None,
        storage: Optional[R2StorageClient] = None,
        pipeline_logger: Optional[PipelineRunLogger] = None,
        error_logger: Optional[ErrorLogger] = None,
        alerter: Optional[EmailAlerter] = None,
        spend_tracker: Optional[SpendTracker] = None,
        replicate_api_token: Optional[str] = None,
        replicate_model_version: Optional[str] = None,
        replicate_client: Optional[Any] = None,
        max_images_per_day: int = 50,
    ) -> None:
        self._db = db or SupabaseClient()
        self._prompt_gen = prompt_generator
        self._storage = storage
        self._pipeline_logger = pipeline_logger or PipelineRunLogger(self._db)
        self._error_logger = error_logger or ErrorLogger(self._db)
        self._alerter = alerter
        self._spend_tracker = spend_tracker
        self._replicate_client = replicate_client
        self._max_images_per_day = max_images_per_day

        cfg = load_config(require_all=False)
        self._replicate_token = replicate_api_token or cfg.replicate.api_token
        self._model_version = replicate_model_version or cfg.replicate.model_version
        self._model_id = cfg.replicate.model_id
        self._image_size = cfg.replicate.image_size

        if not self._replicate_client and self._replicate_token:
            try:
                import replicate
                self._replicate_client = replicate.Client(api_token=self._replicate_token)
            except Exception as exc:
                logger.error("Failed to initialize Replicate client: %s", exc)

    @retry(max_retries=3, service="replicate")
    def _generate_single_image(self, prompt: str) -> bytes:
        """
        Generate a single image via Replicate SDXL.

        Args:
            prompt: The image generation prompt.

        Returns:
            Raw PNG image bytes.
        """
        if not self._replicate_client:
            raise ImageGeneratorError("Replicate client not initialized")

        model_ref = (
            f"{self._model_id}:{self._model_version}"
            if self._model_version
            else self._model_id
        )

        output = self._replicate_client.run(
            model_ref,
            input={
                "prompt": prompt,
                "width": self._image_size,
                "height": self._image_size,
                "num_outputs": 1,
                "output_format": "png",
            },
        )

        # Replicate returns a list of URLs
        if not isinstance(output, list) or len(output) == 0:
            raise ImageGeneratorError(
                f"Replicate returned unexpected output: {type(output).__name__}"
            )

        image_url = str(output[0])
        response = httpx.get(image_url, timeout=60)
        response.raise_for_status()
        image_bytes = response.content

        if len(image_bytes) == 0:
            raise ImageGeneratorError("Replicate returned empty image")

        return image_bytes

    def generate_for_trend(
        self,
        trend: Dict[str, Any],
        run_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        Generate sticker images for a single trend.

        Args:
            trend: Trend dict from Supabase (must have 'id', 'topic').
            run_id: Pipeline run ID for error logging.

        Returns:
            List of created sticker dicts.
        """
        trend_id = trend.get("id", "")
        topic = trend.get("topic", "")
        stickers_created: List[Dict[str, Any]] = []

        # Generate prompts
        if self._prompt_gen:
            try:
                prompts = self._prompt_gen.generate_prompts(topic)
            except Exception as exc:
                logger.error("Prompt generation failed for '%s': %s", topic[:50], exc)
                self._error_logger.log_error(
                    workflow=WORKFLOW_NAME, step="prompt_generation",
                    error_type="api_error", error_message=str(exc),
                    service="openai", pipeline_run_id=run_id,
                    context={"trend_id": trend_id},
                )
                self._update_trend_status(trend_id, "generation_failed")
                return []
        else:
            logger.warning("No prompt generator available, using default prompts")
            from src.stickers.prompt_generator import PromptGenerator as PG
            prompts = PG._fallback_prompts(topic, IMAGES_PER_TREND)

        all_failed = True

        for i, prompt in enumerate(prompts[:IMAGES_PER_TREND]):
            image_bytes = None
            current_prompt = prompt

            # Try generating with quality retries
            for retry_num in range(MAX_QUALITY_RETRIES + 1):
                try:
                    raw_bytes = self._generate_single_image(current_prompt)
                except (RetryExhaustedError, ImageGeneratorError, Exception) as exc:
                    logger.error(
                        "Image generation failed for trend '%s' prompt %d: %s",
                        topic[:50], i + 1, exc,
                    )
                    self._error_logger.log_error(
                        workflow=WORKFLOW_NAME, step="image_generation",
                        error_type="api_error", error_message=str(exc),
                        service="replicate", pipeline_run_id=run_id,
                        context={"trend_id": trend_id, "prompt_index": i},
                    )
                    break

                # Validate quality
                validation = validate_image(raw_bytes)
                if validation.passed:
                    image_bytes = raw_bytes
                    break
                else:
                    logger.warning(
                        "Quality validation failed for trend '%s' prompt %d (retry %d): %s",
                        topic[:50], i + 1, retry_num,
                        "; ".join(validation.failures),
                    )
                    if retry_num < MAX_QUALITY_RETRIES:
                        current_prompt = get_modified_prompt(current_prompt)

            if image_bytes is None:
                logger.warning(
                    "All retries exhausted for trend '%s' prompt %d",
                    topic[:50], i + 1,
                )
                continue

            # Post-process
            try:
                processed = process_image(image_bytes)
            except PostProcessingError as exc:
                logger.error(
                    "Post-processing failed for trend '%s' prompt %d: %s",
                    topic[:50], i + 1, exc,
                )
                continue

            # Upload to R2
            if self._storage:
                try:
                    import uuid
                    sticker_id = str(uuid.uuid4())
                    original_key = f"stickers/{sticker_id}/original.png"
                    print_key = f"stickers/{sticker_id}/print_ready.png"
                    thumb_key = f"stickers/{sticker_id}/thumbnail.png"

                    original_url = self._storage.upload_image(original_key, image_bytes)
                    print_url = self._storage.upload_image(print_key, processed.print_ready)
                    thumb_url = self._storage.upload_image(thumb_key, processed.thumbnail)
                except Exception as exc:
                    logger.error("R2 upload failed: %s", exc)
                    self._error_logger.log_error(
                        workflow=WORKFLOW_NAME, step="image_upload",
                        error_type="api_error", error_message=str(exc),
                        service="r2", pipeline_run_id=run_id,
                        context={"trend_id": trend_id},
                    )
                    continue
            else:
                original_url = ""
                print_url = ""
                thumb_url = ""

            # Create sticker record
            try:
                sticker_data = {
                    "trend_id": trend_id,
                    "title": f"Trending Sticker - {topic[:80]}",
                    "image_url": print_url,
                    "thumbnail_url": thumb_url,
                    "original_url": original_url,
                    "size": "3in",
                    "generation_prompt": current_prompt[:1000],
                    "generation_model": self._model_id,
                    "generation_model_version": self._model_version or "",
                    "moderation_status": "pending",
                }
                sticker = self._db.insert_sticker(sticker_data)
                stickers_created.append(sticker)
                all_failed = False
                logger.info(
                    "Created sticker for trend '%s' (prompt %d)",
                    topic[:50], i + 1,
                )
            except DatabaseError as exc:
                logger.error("Failed to create sticker record: %s", exc)

        # Update trend status
        if stickers_created:
            self._update_trend_status(trend_id, "generated")
        elif all_failed:
            self._update_trend_status(trend_id, "generation_failed")
            if self._alerter:
                self._alerter.send_alert(
                    f"Generation failed for trend: {topic[:50]}",
                    f"All {IMAGES_PER_TREND} image generation attempts failed for trend '{topic}'.",
                    level="warning",
                )

        return stickers_created

    def run(self) -> int:
        """
        Process all discovered trends, generating sticker images.

        Returns:
            Number of stickers generated.
        """
        run_id = self._pipeline_logger.start_run(WORKFLOW_NAME)
        total_stickers = 0
        total_images = 0
        errors_count = 0

        try:
            # Check daily image cap
            # (In production, this would query pipeline_runs for today's count)

            # Check AI budget
            if self._spend_tracker:
                budget = self._spend_tracker.check_budget()
                if not budget["can_proceed"]:
                    logger.warning("AI budget exceeded, skipping generation")
                    self._pipeline_logger.complete_run(
                        run_id, metadata={"skipped": "budget_exceeded"},
                    )
                    return 0

            # Fetch discovered trends
            trends = self._db.get_trends_by_status("discovered")
            if not trends:
                logger.info("No discovered trends to process")
                self._pipeline_logger.complete_run(
                    run_id, counts={"stickers_generated": 0},
                )
                return 0

            logger.info("Processing %d discovered trends", len(trends))

            for trend in trends:
                stickers = self.generate_for_trend(trend, run_id=run_id)
                total_stickers += len(stickers)
                total_images += len(stickers)

                if total_images >= self._max_images_per_day:
                    logger.info("Daily image cap reached (%d)", self._max_images_per_day)
                    break

            ai_cost = estimate_replicate_cost(total_images)
            self._pipeline_logger.complete_run(
                run_id,
                counts={"stickers_generated": total_stickers, "errors_count": errors_count},
                ai_cost_estimate_usd=ai_cost,
            )

        except Exception as exc:
            logger.error("Sticker generator failed: %s", exc)
            self._pipeline_logger.fail_run(run_id, error_message=str(exc))
            raise

        logger.info("Generation complete: %d stickers created", total_stickers)
        return total_stickers

    def _update_trend_status(self, trend_id: str, status: str) -> None:
        """Update trend status in Supabase."""
        try:
            self._db.update_trend(trend_id, {"status": status})
        except DatabaseError as exc:
            logger.error("Failed to update trend %s status: %s", trend_id, exc)


def main() -> None:
    """Entry point for `python -m src.stickers.image_generator`."""
    setup_logging()
    logger.info("Starting sticker generator")

    try:
        cfg = load_config()
    except Exception as exc:
        logger.critical("Failed to load config: %s", exc)
        sys.exit(1)

    db = SupabaseClient()
    generator = ImageGenerator(
        db=db,
        prompt_generator=PromptGenerator(),
        storage=R2StorageClient(),
        alerter=EmailAlerter(),
        spend_tracker=SpendTracker(db=db),
        max_images_per_day=cfg.caps.max_images_per_day,
    )

    try:
        count = generator.run()
        logger.info("Sticker generator finished: %d stickers", count)
        sys.exit(0)
    except Exception as exc:
        logger.critical("Sticker generator failed: %s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
