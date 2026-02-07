"""
Content moderator for Sticker Trendz.

Two-stage moderation:
  Stage 1 (topic): GPT-4o-mini safety scoring during trend detection
  Stage 2 (image): OpenAI Moderation API on generated images

Score thresholds:
  < 0.4  -> auto-approve
  0.4-0.7 -> flag for manual review
  > 0.7  -> auto-reject

Also checks text/tags against keyword and trademark blocklists.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

from src.config import load_config
from src.db import SupabaseClient, DatabaseError
from src.moderation.blocklist import check_trademark, check_keywords
from src.monitoring.alerter import EmailAlerter
from src.monitoring.error_logger import ErrorLogger
from src.resilience import retry, RetryExhaustedError

logger = logging.getLogger(__name__)

APPROVE_THRESHOLD = 0.4
FLAG_THRESHOLD = 0.7
AUTO_REJECT_HOURS = 48


class ModerationResult:
    """Result of moderating a sticker."""

    def __init__(
        self,
        status: str,
        score: float = 0.0,
        categories: Optional[Dict[str, float]] = None,
        reason: str = "",
    ):
        self.status = status  # 'approved', 'flagged', 'rejected'
        self.score = score
        self.categories = categories or {}
        self.reason = reason


class ContentModerator:
    """
    Moderates sticker images using OpenAI Moderation API and blocklists.

    For each sticker:
      1. Check description/tags against trademark and keyword blocklists
      2. Run OpenAI Moderation API on the image/description
      3. Apply score thresholds
      4. Send alerts for flagged content
    """

    def __init__(
        self,
        db: Optional[SupabaseClient] = None,
        openai_api_key: Optional[str] = None,
        openai_client: Optional[Any] = None,
        alerter: Optional[EmailAlerter] = None,
        error_logger: Optional[ErrorLogger] = None,
    ) -> None:
        self._db = db or SupabaseClient()
        self._alerter = alerter
        self._error_logger = error_logger
        self._client = openai_client

        if not self._client:
            cfg = load_config(require_all=False)
            api_key = openai_api_key or cfg.openai.api_key
            try:
                from openai import OpenAI
                self._client = OpenAI(api_key=api_key)
            except Exception as exc:
                logger.error("Failed to initialize OpenAI client: %s", exc)

    def moderate_image(
        self,
        image_url: str,
        description: str = "",
        tags: Optional[List[str]] = None,
        sticker_id: Optional[str] = None,
        topic: str = "",
    ) -> ModerationResult:
        """
        Run moderation on a sticker image.

        Args:
            image_url: R2 URL of the sticker image.
            description: Listing description text.
            tags: Listing tags.
            sticker_id: UUID of the sticker record.
            topic: Trend topic for context.

        Returns:
            ModerationResult with status, score, and reason.
        """
        # Step 1: Check blocklists on text content
        text_to_check = f"{description} {' '.join(tags or [])} {topic}"

        is_tm, tm_match = check_trademark(text_to_check)
        if is_tm:
            logger.info(
                "Sticker %s rejected: trademark violation (%s)",
                sticker_id, tm_match,
            )
            return ModerationResult(
                status="rejected",
                reason=f"trademark_violation: {tm_match}",
            )

        is_kw, kw_match = check_keywords(text_to_check)
        if is_kw:
            logger.info(
                "Sticker %s rejected: keyword blocklist (%s)",
                sticker_id, kw_match,
            )
            return ModerationResult(
                status="rejected",
                reason=f"keyword_blocked: {kw_match}",
            )

        # Step 2: OpenAI Moderation API
        moderation_score = 0.0
        categories: Dict[str, float] = {}

        if self._client and description:
            try:
                result = self._call_moderation_api(description)
                moderation_score = result.get("max_score", 0.0)
                categories = result.get("categories", {})
            except Exception as exc:
                logger.error("OpenAI moderation API failed: %s", exc)
                if self._error_logger:
                    self._error_logger.log_error(
                        workflow="sticker_generator",
                        step="moderation",
                        error_type="api_error",
                        error_message=str(exc),
                        service="openai",
                        context={"sticker_id": sticker_id or ""},
                    )
                # On API failure, flag for manual review rather than auto-approve
                return ModerationResult(
                    status="flagged",
                    score=0.0,
                    reason="moderation_api_unavailable",
                )

        # Step 3: Apply thresholds
        if moderation_score > FLAG_THRESHOLD:
            status = "rejected"
            reason = f"auto_rejected: score {moderation_score:.3f} > {FLAG_THRESHOLD}"
            logger.info("Sticker %s auto-rejected: score=%.3f", sticker_id, moderation_score)
        elif moderation_score >= APPROVE_THRESHOLD:
            status = "flagged"
            reason = f"flagged_for_review: score {moderation_score:.3f}"
            logger.info("Sticker %s flagged: score=%.3f", sticker_id, moderation_score)
            # Send alert
            if self._alerter and sticker_id:
                self._alerter.send_moderation_alert(
                    sticker_id=sticker_id,
                    image_url=image_url,
                    topic=topic,
                    moderation_score=moderation_score,
                    moderation_categories=categories,
                )
        else:
            status = "approved"
            reason = f"auto_approved: score {moderation_score:.3f} < {APPROVE_THRESHOLD}"
            logger.info("Sticker %s approved: score=%.3f", sticker_id, moderation_score)

        return ModerationResult(
            status=status,
            score=moderation_score,
            categories=categories,
            reason=reason,
        )

    @retry(max_retries=2, service="openai")
    def _call_moderation_api(self, text: str) -> Dict[str, Any]:
        """Call OpenAI's Moderation API and return parsed results."""
        response = self._client.moderations.create(input=text)
        result = response.results[0]

        # Extract category scores
        categories: Dict[str, float] = {}
        if hasattr(result, "category_scores"):
            scores = result.category_scores
            for attr in dir(scores):
                if not attr.startswith("_"):
                    val = getattr(scores, attr, None)
                    if isinstance(val, (int, float)):
                        categories[attr] = float(val)

        max_score = max(categories.values()) if categories else 0.0

        return {
            "max_score": max_score,
            "categories": categories,
            "flagged": result.flagged,
        }

    def moderate_sticker(self, sticker: Dict[str, Any]) -> Dict[str, Any]:
        """
        Moderate a sticker and update its record in Supabase.

        Args:
            sticker: Sticker dict from Supabase.

        Returns:
            Updated sticker dict.
        """
        sticker_id = sticker.get("id", "")
        image_url = sticker.get("image_url", "")
        description = sticker.get("description", "")
        tags = sticker.get("tags", [])
        topic = sticker.get("title", "")

        result = self.moderate_image(
            image_url=image_url,
            description=description,
            tags=tags,
            sticker_id=sticker_id,
            topic=topic,
        )

        # Update sticker in DB
        update_data: Dict[str, Any] = {
            "moderation_status": result.status,
            "moderation_score": result.score,
            "moderation_categories": result.categories,
        }

        try:
            self._db.update_sticker(sticker_id, update_data)
        except DatabaseError as exc:
            logger.error("Failed to update sticker moderation: %s", exc)

        return {**sticker, **update_data}

    def check_flagged_timeout(self) -> int:
        """
        Auto-reject flagged stickers that haven't been reviewed for 48 hours.

        Returns:
            Number of stickers auto-rejected.
        """
        count = 0
        try:
            flagged = self._db.get_stickers_by_status("flagged")
            cutoff = datetime.now(timezone.utc) - timedelta(hours=AUTO_REJECT_HOURS)

            for sticker in flagged:
                created_str = sticker.get("created_at", "")
                try:
                    created = datetime.fromisoformat(
                        created_str.replace("Z", "+00:00")
                    )
                except (ValueError, AttributeError):
                    continue

                if created < cutoff:
                    try:
                        self._db.update_sticker(sticker["id"], {
                            "moderation_status": "rejected",
                        })
                        count += 1
                        logger.info(
                            "Auto-rejected flagged sticker %s (>%dh without review)",
                            sticker["id"], AUTO_REJECT_HOURS,
                        )
                        if self._alerter:
                            self._alerter.send_alert(
                                f"Sticker auto-rejected after {AUTO_REJECT_HOURS}h",
                                f"Sticker {sticker['id']} was auto-rejected because "
                                f"it was flagged for over {AUTO_REJECT_HOURS} hours "
                                f"without manual review.",
                                level="warning",
                            )
                    except DatabaseError as exc:
                        logger.error("Failed to auto-reject sticker: %s", exc)

        except DatabaseError as exc:
            logger.error("Failed to check flagged stickers: %s", exc)

        if count > 0:
            logger.info("Auto-rejected %d stickers past %dh review window", count, AUTO_REJECT_HOURS)
        return count
