"""
Integration test for Resend email alerter.

Requires RESEND_API_KEY and ALERT_EMAIL to be set in .env.
Run with: pytest tests/integration/test_resend_email.py -v -s
"""

import pytest
from src.config import load_config
from src.monitoring.alerter import EmailAlerter


@pytest.mark.integration
def test_resend_send_alert():
    """Send a real test email via Resend and verify no exception is raised."""
    cfg = load_config(require_all=False)

    assert cfg.notification.resend_api_key, "RESEND_API_KEY is not set in .env"
    assert cfg.notification.alert_email, "ALERT_EMAIL is not set in .env"

    alerter = EmailAlerter(
        resend_api_key=cfg.notification.resend_api_key,
        alert_email=cfg.notification.alert_email,
    )

    # send_alert is best-effort and swallows AlerterError, so call _send_email
    # directly to surface any real API errors
    alerter._send_email(
        subject="[Sticker Trendz] Integration test — Resend",
        body=(
            "This is an automated integration test confirming that Resend\n"
            "email delivery is working correctly for Sticker Trendz alerts.\n\n"
            "You can ignore this email."
        ),
    )
