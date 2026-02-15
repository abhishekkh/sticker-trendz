"""
Email alerter for Sticker Trendz using Resend.

Sends critical alerts, warning alerts, daily summary emails, and
moderation review alerts. Email content never includes PII.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from src.config import load_config
from src.monitoring.error_logger import sanitize_string

logger = logging.getLogger(__name__)


class AlerterError(Exception):
    """Raised when an alert fails to send."""


class EmailAlerter:
    """
    Resend-based email alerter for operational notifications.

    Supports:
      - Critical alerts (OAuth failure, all APIs down, DB down)
      - Warning alerts (rate limits approaching, high error rate)
      - Daily summary emails
      - Moderation review alerts (flagged sticker images)

    All alerts are best-effort -- failures are logged but never halt
    the main workflow.
    """

    def __init__(
        self,
        resend_api_key: Optional[str] = None,
        alert_email: Optional[str] = None,
        from_email: str = "onboarding@resend.dev",
        _send_fn: Optional[Any] = None,
    ) -> None:
        """
        Args:
            resend_api_key: Resend API key. Falls back to config.
            alert_email: Recipient for alerts. Falls back to config.
            from_email: Sender address.
            _send_fn: Injectable send function for testing.
        """
        cfg = load_config(require_all=False)
        self._api_key = resend_api_key or cfg.notification.resend_api_key
        self._alert_email = alert_email or cfg.notification.alert_email
        self._from_email = from_email
        self._send_fn = _send_fn

    def _send_email(self, subject: str, body: str) -> None:
        """
        Send an email via Resend.

        If a custom _send_fn was injected (testing), use that instead.
        """
        if self._send_fn is not None:
            self._send_fn(subject=subject, body=body, to_email=self._alert_email)
            return

        try:
            import resend

            resend.api_key = self._api_key
            resend.Emails.send({
                "from": self._from_email,
                "to": [self._alert_email],
                "subject": subject,
                "text": body,
            })
            logger.info("Email sent: subject='%s'", subject)
        except Exception as exc:
            logger.error(
                "Failed to send email via Resend: %s (subject='%s')",
                exc, subject,
            )
            raise AlerterError(f"Resend send failed: {exc}") from exc

    def send_alert(
        self, subject: str, body: str, level: str = "critical"
    ) -> None:
        """
        Send an operational alert email.

        Args:
            subject: Email subject line.
            body: Plain-text email body (will be sanitized).
            level: Alert severity ('critical', 'warning', 'info').
        """
        prefix = f"[Sticker Trendz {level.upper()}]"
        full_subject = f"{prefix} {subject}"
        safe_body = sanitize_string(body)

        try:
            self._send_email(full_subject, safe_body)
        except AlerterError:
            # Alerts are best-effort; log but do not propagate
            logger.warning("Alert email failed (best-effort): %s", subject)

    def send_moderation_alert(
        self,
        sticker_id: str,
        image_url: str,
        topic: str,
        moderation_score: float,
        moderation_categories: Optional[Dict[str, float]] = None,
    ) -> None:
        """
        Send a moderation review alert for a flagged sticker.

        Args:
            sticker_id: UUID of the flagged sticker.
            image_url: R2 URL of the sticker image.
            topic: Trend topic the sticker is based on.
            moderation_score: Overall moderation score (0.0-1.0).
            moderation_categories: Score breakdown by category.
        """
        subject = f"Flagged sticker needs review: {topic}"
        categories_text = ""
        if moderation_categories:
            categories_text = "\n".join(
                f"  - {cat}: {score:.3f}"
                for cat, score in moderation_categories.items()
            )

        body = (
            f"A sticker has been flagged for manual review.\n\n"
            f"Sticker ID: {sticker_id}\n"
            f"Topic: {topic}\n"
            f"Image URL: {image_url}\n"
            f"Moderation Score: {moderation_score:.3f}\n"
        )
        if categories_text:
            body += f"\nCategory Breakdown:\n{categories_text}\n"
        body += (
            f"\nAction required: Review and approve/reject in the Supabase dashboard.\n"
            f"Auto-reject will occur in 48 hours if no action is taken."
        )

        self.send_alert(subject, body, level="warning")

    def send_daily_summary(
        self,
        pipeline_health: Dict[str, Any],
        revenue: Dict[str, Any],
        pricing: Dict[str, Any],
        costs: Dict[str, Any],
        alerts: List[str],
    ) -> None:
        """
        Send the daily summary email per the spec template.

        Args:
            pipeline_health: Dict with workflow statuses and counts.
            revenue: Dict with orders, gross_revenue, profit, etc.
            pricing: Dict with repriced, archived, below_floor counts.
            costs: Dict with ai_spend, api_calls, listing_fees.
            alerts: List of alert strings from the day.
        """
        subject = "Daily Summary"

        sections: list[str] = []

        # Pipeline Health
        sections.append("=== Pipeline Health ===")
        for key, val in pipeline_health.items():
            sections.append(f"  {key}: {val}")

        # Revenue
        sections.append("\n=== Revenue ===")
        sections.append(f"  Orders: {revenue.get('orders', 0)}")
        sections.append(f"  Gross Revenue: ${revenue.get('gross_revenue', 0):.2f}")
        sections.append(f"  COGS: ${revenue.get('cogs', 0):.2f}")
        sections.append(f"  Etsy Fees: ${revenue.get('etsy_fees', 0):.2f}")
        sections.append(f"  Est. Profit: ${revenue.get('estimated_profit', 0):.2f}")
        sections.append(f"  Avg Order Value: ${revenue.get('avg_order_value', 0):.2f}")

        # Pricing
        sections.append("\n=== Pricing ===")
        sections.append(f"  Stickers Repriced: {pricing.get('repriced', 0)}")
        sections.append(f"  Stickers Archived: {pricing.get('archived', 0)}")
        sections.append(f"  Below Floor Price: {pricing.get('below_floor', 0)}")
        sections.append(
            f"  Active Listings: {pricing.get('active_listings', 0)} / "
            f"{pricing.get('max_listings', 300)}"
        )

        # Costs
        sections.append("\n=== Costs ===")
        sections.append(f"  AI Spend Today: ${costs.get('ai_spend', 0):.2f}")
        sections.append(f"  AI Spend MTD: ${costs.get('ai_spend_mtd', 0):.2f}")
        sections.append(f"  Etsy API Calls: {costs.get('api_calls', 0)}")
        sections.append(f"  Listing Fees: ${costs.get('listing_fees', 0):.2f}")

        # Alerts
        if alerts:
            sections.append("\n=== Alerts ===")
            for alert in alerts:
                sections.append(f"  - {alert}")
        else:
            sections.append("\n=== Alerts ===")
            sections.append("  No alerts today.")

        body = "\n".join(sections)

        try:
            self._send_email(
                f"[Sticker Trendz] {subject}",
                body,
            )
        except AlerterError:
            logger.warning("Daily summary email failed (best-effort)")

    def send_oauth_failure_alert(self, shop_id: str, error_detail: str) -> None:
        """
        Send a critical alert when Etsy OAuth refresh fails with invalid_grant.
        """
        subject = f"Etsy OAuth FAILED - manual re-authorization required (shop {shop_id})"
        body = (
            f"The Etsy OAuth token refresh for shop '{shop_id}' failed with an "
            f"invalid_grant error. All Etsy-dependent workflows are now halted.\n\n"
            f"Error: {sanitize_string(error_detail)}\n\n"
            f"Action required:\n"
            f"1. Re-authorize via the Etsy OAuth flow\n"
            f"2. Update tokens in the Supabase etsy_tokens table\n"
            f"3. Manually trigger a test workflow to verify"
        )
        self.send_alert(subject, body, level="critical")

    def send_budget_warning(self, monthly_spend: float, cap: float) -> None:
        """Send a warning when AI spend approaches the monthly budget cap."""
        subject = f"AI spend warning: ${monthly_spend:.2f} / ${cap:.2f}"
        body = (
            f"Monthly AI spend has reached ${monthly_spend:.2f}, "
            f"which is {(monthly_spend / cap * 100):.1f}% of the ${cap:.2f} cap.\n\n"
            f"If spend reaches ${cap:.2f}, all AI operations will be halted."
        )
        level = "critical" if monthly_spend >= cap else "warning"
        self.send_alert(subject, body, level=level)

    def send_rate_limit_alert(self, daily_calls: int, threshold: int = 9500) -> None:
        """Send an alert when Etsy API usage hits the hard stop threshold."""
        subject = f"Etsy API rate limit: {daily_calls} calls used today"
        body = (
            f"Daily Etsy API call count has reached {daily_calls}, "
            f"exceeding the hard-stop threshold of {threshold}.\n\n"
            f"All Etsy API calls are halted until midnight UTC."
        )
        self.send_alert(subject, body, level="critical")
