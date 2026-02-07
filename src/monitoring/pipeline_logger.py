"""
Pipeline run logger for Sticker Trendz.

Records every workflow execution to the pipeline_runs table in Supabase.
Tracks workflow name, status, timing, counts, API usage, and AI cost.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from src.db import SupabaseClient, DatabaseError

logger = logging.getLogger(__name__)


class PipelineRunLogger:
    """
    Logger that records pipeline run lifecycle to the pipeline_runs table.

    Usage:
        run_logger = PipelineRunLogger(db)
        run_id = run_logger.start_run("trend_monitor")
        # ... do work ...
        run_logger.complete_run(run_id, counts={...})
        # or on failure:
        run_logger.fail_run(run_id, error_message="something broke")
    """

    # Cost estimates per the spec
    GPT4O_MINI_INPUT_COST_PER_TOKEN: float = 0.15 / 1_000_000  # $0.15 per 1M input tokens
    GPT4O_MINI_OUTPUT_COST_PER_TOKEN: float = 0.60 / 1_000_000  # $0.60 per 1M output tokens
    REPLICATE_COST_PER_IMAGE: float = 0.04  # ~$0.02-$0.05 per image, use midpoint

    def __init__(self, db: Optional[SupabaseClient] = None) -> None:
        self._db = db or SupabaseClient()
        self._start_times: Dict[str, float] = {}

    def start_run(self, workflow: str, metadata: Optional[Dict[str, Any]] = None) -> str:
        """
        Create a new pipeline_runs row with status='started'.

        Args:
            workflow: Name of the workflow (e.g. 'trend_monitor', 'pricing_engine').
            metadata: Optional additional metadata for this run.

        Returns:
            The run ID (UUID string).
        """
        now = datetime.now(timezone.utc).isoformat()
        data: Dict[str, Any] = {
            "workflow": workflow,
            "status": "started",
            "started_at": now,
        }
        if metadata:
            data["metadata"] = metadata

        try:
            result = self._db.insert_pipeline_run(data)
            run_id = result.get("id", "")
            self._start_times[run_id] = time.monotonic()
            logger.info("Pipeline run started: workflow=%s, run_id=%s", workflow, run_id)
            return run_id
        except DatabaseError as exc:
            logger.error("Failed to start pipeline run for '%s': %s", workflow, exc)
            raise

    def complete_run(
        self,
        run_id: str,
        counts: Optional[Dict[str, int]] = None,
        etsy_api_calls_used: int = 0,
        ai_cost_estimate_usd: float = 0.0,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        Mark a pipeline run as completed with final metrics.

        Args:
            run_id: The pipeline run ID returned by start_run().
            counts: Dict of count fields (trends_found, stickers_generated, etc.).
            etsy_api_calls_used: Number of Etsy API calls consumed this run.
            ai_cost_estimate_usd: Estimated AI cost for this run.
            metadata: Optional additional metadata to merge.
        """
        now = datetime.now(timezone.utc).isoformat()
        duration = self._calculate_duration(run_id)

        data: Dict[str, Any] = {
            "status": "completed",
            "ended_at": now,
            "duration_seconds": duration,
            "etsy_api_calls_used": etsy_api_calls_used,
            "ai_cost_estimate_usd": ai_cost_estimate_usd,
        }

        if counts:
            for key, value in counts.items():
                data[key] = value

        if metadata:
            data["metadata"] = metadata

        try:
            self._db.update_pipeline_run(run_id, data)
            logger.info(
                "Pipeline run completed: run_id=%s, duration=%ds, api_calls=%d, ai_cost=$%.4f",
                run_id, duration or 0, etsy_api_calls_used, ai_cost_estimate_usd,
            )
        except DatabaseError as exc:
            logger.error("Failed to complete pipeline run %s: %s", run_id, exc)
            raise

    def fail_run(
        self,
        run_id: str,
        error_message: str,
        counts: Optional[Dict[str, int]] = None,
        etsy_api_calls_used: int = 0,
        ai_cost_estimate_usd: float = 0.0,
    ) -> None:
        """
        Mark a pipeline run as failed.

        Args:
            run_id: The pipeline run ID.
            error_message: Description of the failure.
            counts: Any partial counts to record.
            etsy_api_calls_used: API calls consumed before failure.
            ai_cost_estimate_usd: AI cost incurred before failure.
        """
        now = datetime.now(timezone.utc).isoformat()
        duration = self._calculate_duration(run_id)

        data: Dict[str, Any] = {
            "status": "failed",
            "ended_at": now,
            "duration_seconds": duration,
            "etsy_api_calls_used": etsy_api_calls_used,
            "ai_cost_estimate_usd": ai_cost_estimate_usd,
            "metadata": {"error": error_message},
        }

        if counts:
            for key, value in counts.items():
                data[key] = value

        try:
            self._db.update_pipeline_run(run_id, data)
            logger.error(
                "Pipeline run failed: run_id=%s, duration=%ds, error=%s",
                run_id, duration or 0, error_message,
            )
        except DatabaseError as exc:
            logger.error("Failed to record pipeline run failure %s: %s", run_id, exc)
            raise

    def partial_run(
        self,
        run_id: str,
        counts: Optional[Dict[str, int]] = None,
        error_message: str = "",
        etsy_api_calls_used: int = 0,
        ai_cost_estimate_usd: float = 0.0,
    ) -> None:
        """
        Mark a pipeline run as partially completed (some items succeeded, some failed).

        Args:
            run_id: The pipeline run ID.
            counts: Count fields reflecting partial progress.
            error_message: Summary of what failed.
            etsy_api_calls_used: API calls consumed.
            ai_cost_estimate_usd: AI cost incurred.
        """
        now = datetime.now(timezone.utc).isoformat()
        duration = self._calculate_duration(run_id)

        data: Dict[str, Any] = {
            "status": "partial",
            "ended_at": now,
            "duration_seconds": duration,
            "etsy_api_calls_used": etsy_api_calls_used,
            "ai_cost_estimate_usd": ai_cost_estimate_usd,
        }

        if error_message:
            data["metadata"] = {"error": error_message}

        if counts:
            for key, value in counts.items():
                data[key] = value

        try:
            self._db.update_pipeline_run(run_id, data)
            logger.warning(
                "Pipeline run partial: run_id=%s, duration=%ds, error=%s",
                run_id, duration or 0, error_message,
            )
        except DatabaseError as exc:
            logger.error("Failed to record partial pipeline run %s: %s", run_id, exc)
            raise

    @staticmethod
    def estimate_ai_cost(
        input_tokens: int = 0,
        output_tokens: int = 0,
        images_generated: int = 0,
    ) -> float:
        """
        Calculate estimated AI cost from token and image counts.

        Args:
            input_tokens: Total GPT-4o-mini input tokens.
            output_tokens: Total GPT-4o-mini output tokens.
            images_generated: Number of Replicate images generated.

        Returns:
            Estimated cost in USD.
        """
        openai_cost = (
            input_tokens * PipelineRunLogger.GPT4O_MINI_INPUT_COST_PER_TOKEN
            + output_tokens * PipelineRunLogger.GPT4O_MINI_OUTPUT_COST_PER_TOKEN
        )
        replicate_cost = images_generated * PipelineRunLogger.REPLICATE_COST_PER_IMAGE
        return round(openai_cost + replicate_cost, 4)

    def _calculate_duration(self, run_id: str) -> Optional[int]:
        """Calculate duration in seconds from the stored start time."""
        start = self._start_times.pop(run_id, None)
        if start is not None:
            return int(time.monotonic() - start)
        return None
