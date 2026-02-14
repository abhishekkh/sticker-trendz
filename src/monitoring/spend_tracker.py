"""
AI spend tracking and budget enforcement for Sticker Trendz.

Estimates OpenAI and Replicate costs, tracks cumulative daily and monthly
spend, and enforces budget caps ($120 warning, $150 hard stop per month).
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Optional

from src.db import SupabaseClient, DatabaseError
from src.monitoring.alerter import EmailAlerter

logger = logging.getLogger(__name__)

# LLM per-token costs – configurable via env vars.
# Default to 0.0 for Gemini Flash free tier; set LLM_INPUT_COST_PER_TOKEN
# and LLM_OUTPUT_COST_PER_TOKEN if using a paid model (e.g. GPT-4o-mini).
def _load_llm_cost(env_var: str, default: float) -> float:
    raw = os.getenv(env_var)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning(
            "%s has non-numeric value '%s', using default %f", env_var, raw, default,
        )
        return default


LLM_INPUT_COST_PER_TOKEN: float = _load_llm_cost("LLM_INPUT_COST_PER_TOKEN", 0.0)
LLM_OUTPUT_COST_PER_TOKEN: float = _load_llm_cost("LLM_OUTPUT_COST_PER_TOKEN", 0.0)

# Backward-compat aliases (tests/callers that import the old names)
GPT4O_MINI_INPUT_COST_PER_TOKEN: float = LLM_INPUT_COST_PER_TOKEN
GPT4O_MINI_OUTPUT_COST_PER_TOKEN: float = LLM_OUTPUT_COST_PER_TOKEN

# Replicate cost per image – configurable via env var (default: FLUX Schnell ~$0.003)
def _load_replicate_cost() -> float:
    raw = os.getenv("REPLICATE_COST_PER_IMAGE")
    if raw is None:
        return 0.003
    try:
        return float(raw)
    except ValueError:
        logger.warning(
            "REPLICATE_COST_PER_IMAGE has non-numeric value '%s', using default 0.003", raw,
        )
        return 0.003

REPLICATE_COST_PER_IMAGE: float = _load_replicate_cost()

# Budget thresholds
MONTHLY_WARNING_USD: float = 120.0
MONTHLY_HARD_STOP_USD: float = 150.0
DAILY_WARNING_USD: float = 8.0


def estimate_llm_cost(input_tokens: int, output_tokens: int) -> float:
    """
    Estimate LLM API cost based on configured per-token rates.

    Defaults to 0.0 (Gemini Flash free tier). Override via
    LLM_INPUT_COST_PER_TOKEN / LLM_OUTPUT_COST_PER_TOKEN env vars.

    Args:
        input_tokens: Number of input tokens.
        output_tokens: Number of output tokens.

    Returns:
        Estimated cost in USD.
    """
    return round(
        input_tokens * LLM_INPUT_COST_PER_TOKEN
        + output_tokens * LLM_OUTPUT_COST_PER_TOKEN,
        6,
    )


# Backward-compat alias
estimate_openai_cost = estimate_llm_cost


def estimate_replicate_cost(image_count: int) -> float:
    """
    Estimate Replicate image generation cost.

    Args:
        image_count: Number of images generated.

    Returns:
        Estimated cost in USD.
    """
    return round(image_count * REPLICATE_COST_PER_IMAGE, 4)


class SpendTracker:
    """
    Tracks AI spend against daily and monthly budget caps.

    Reads cumulative costs from the pipeline_runs table and enforces
    the $120 warning / $150 hard-stop thresholds.
    """

    def __init__(
        self,
        db: Optional[SupabaseClient] = None,
        alerter: Optional[EmailAlerter] = None,
        monthly_warning: float = MONTHLY_WARNING_USD,
        monthly_cap: float = MONTHLY_HARD_STOP_USD,
        daily_warning: float = DAILY_WARNING_USD,
    ) -> None:
        self._db = db or SupabaseClient()
        self._alerter = alerter or EmailAlerter()
        self._monthly_warning = monthly_warning
        self._monthly_cap = monthly_cap
        self._daily_warning = daily_warning
        self._alert_sent_for_month: Optional[str] = None  # Track if we already alerted this month

    def get_daily_spend(self, date: Optional[datetime] = None) -> float:
        """
        Sum ai_cost_estimate_usd from pipeline_runs for a given date.

        Args:
            date: Date to query. Defaults to today (UTC).

        Returns:
            Total AI spend in USD for the day.
        """
        target = date or datetime.now(timezone.utc)
        date_str = target.strftime("%Y-%m-%d")

        try:
            rows = self._db.select(
                "pipeline_runs",
                columns="ai_cost_estimate_usd",
            )
            total = 0.0
            for row in rows:
                started = row.get("started_at", "")
                if isinstance(started, str) and started.startswith(date_str):
                    cost = row.get("ai_cost_estimate_usd")
                    if cost is not None:
                        total += float(cost)
            return round(total, 4)
        except DatabaseError as exc:
            logger.error("Failed to query daily AI spend: %s", exc)
            return 0.0

    def get_monthly_spend(self, year: Optional[int] = None, month: Optional[int] = None) -> float:
        """
        Sum ai_cost_estimate_usd from pipeline_runs for a given month.

        Args:
            year: Year to query. Defaults to current year.
            month: Month to query. Defaults to current month.

        Returns:
            Total AI spend in USD for the month.
        """
        now = datetime.now(timezone.utc)
        y = year or now.year
        m = month or now.month
        prefix = f"{y}-{m:02d}"

        try:
            rows = self._db.select(
                "pipeline_runs",
                columns="ai_cost_estimate_usd,started_at",
            )
            total = 0.0
            for row in rows:
                started = row.get("started_at", "")
                if isinstance(started, str) and started.startswith(prefix):
                    cost = row.get("ai_cost_estimate_usd")
                    if cost is not None:
                        total += float(cost)
            return round(total, 4)
        except DatabaseError as exc:
            logger.error("Failed to query monthly AI spend: %s", exc)
            return 0.0

    def check_budget(self) -> dict:
        """
        Check current monthly spend against budget thresholds.

        Sends alert emails:
        - At $120 warning threshold
        - At $150 hard stop threshold

        Returns:
            Dict with keys:
              - can_proceed (bool): True if under the hard stop cap.
              - monthly_spend (float): Current month's total.
              - warning (bool): True if past the warning threshold.
              - hard_stop (bool): True if past the hard stop threshold.
              - message (str): Human-readable status.
        """
        monthly = self.get_monthly_spend()
        hard_stop = monthly >= self._monthly_cap
        warning = monthly >= self._monthly_warning

        # Track current month to avoid duplicate alerts
        now = datetime.now(timezone.utc)
        current_month = f"{now.year}-{now.month:02d}"

        if hard_stop:
            message = (
                f"HARD STOP: Monthly AI spend ${monthly:.2f} exceeds "
                f"cap ${self._monthly_cap:.2f}. All AI operations halted."
            )
            logger.critical(message)
            # Send hard stop alert (only once per month)
            if self._alert_sent_for_month != current_month:
                try:
                    self._alerter.send_budget_warning(monthly, self._monthly_cap)
                    self._alert_sent_for_month = current_month
                except Exception as exc:
                    logger.error("Failed to send budget hard stop alert: %s", exc)
        elif warning:
            message = (
                f"WARNING: Monthly AI spend ${monthly:.2f} approaching "
                f"cap ${self._monthly_cap:.2f}."
            )
            logger.warning(message)
            # Send warning alert (only once per month)
            if self._alert_sent_for_month != current_month:
                try:
                    self._alerter.send_budget_warning(monthly, self._monthly_cap)
                    self._alert_sent_for_month = current_month
                except Exception as exc:
                    logger.error("Failed to send budget warning alert: %s", exc)
        else:
            message = f"Monthly AI spend: ${monthly:.2f} / ${self._monthly_cap:.2f}"
            logger.info(message)

        return {
            "can_proceed": not hard_stop,
            "monthly_spend": monthly,
            "warning": warning,
            "hard_stop": hard_stop,
            "message": message,
        }

    def check_daily_budget(self) -> dict:
        """
        Check current daily spend against the daily warning threshold.

        Sends alert email if daily spend exceeds $8.

        Returns:
            Dict with keys:
              - daily_spend (float): Today's total.
              - warning (bool): True if past the daily warning.
              - message (str): Human-readable status.
        """
        daily = self.get_daily_spend()
        warning = daily >= self._daily_warning

        if warning:
            message = (
                f"WARNING: Daily AI spend ${daily:.2f} exceeds "
                f"threshold ${self._daily_warning:.2f}."
            )
            logger.warning(message)
            # Send daily warning alert
            try:
                subject = f"Daily AI spend warning: ${daily:.2f}"
                body = (
                    f"Daily AI spend has reached ${daily:.2f}, "
                    f"exceeding the ${self._daily_warning:.2f} warning threshold.\n\n"
                    f"Please review pipeline runs to ensure costs are under control."
                )
                self._alerter.send_alert(subject, body, level="warning")
            except Exception as exc:
                logger.error("Failed to send daily spend warning alert: %s", exc)
        else:
            message = f"Daily AI spend: ${daily:.2f} / ${self._daily_warning:.2f}"

        return {
            "daily_spend": daily,
            "warning": warning,
            "message": message,
        }
