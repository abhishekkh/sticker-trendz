"""Tests for src/monitoring/spend_tracker.py -- AI spend tracking and budget enforcement."""

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch, call
import pytest

from src.monitoring.spend_tracker import (
    SpendTracker,
    estimate_openai_cost,
    estimate_llm_cost,
    estimate_replicate_cost,
    LLM_INPUT_COST_PER_TOKEN,
    LLM_OUTPUT_COST_PER_TOKEN,
    GPT4O_MINI_INPUT_COST_PER_TOKEN,
    GPT4O_MINI_OUTPUT_COST_PER_TOKEN,
    REPLICATE_COST_PER_IMAGE,
    MONTHLY_WARNING_USD,
    MONTHLY_HARD_STOP_USD,
    DAILY_WARNING_USD,
)
from src.db import DatabaseError


class TestEstimateFunctions:
    """Tests for the standalone cost estimation functions."""

    def test_estimate_llm_cost_defaults_to_zero(self):
        """Default LLM cost should be 0.0 (Gemini Flash free tier)."""
        assert LLM_INPUT_COST_PER_TOKEN == 0.0
        assert LLM_OUTPUT_COST_PER_TOKEN == 0.0

        cost = estimate_llm_cost(1_000_000, 1_000_000)
        assert cost == 0.0

    def test_estimate_openai_cost_is_alias(self):
        """estimate_openai_cost should be an alias for estimate_llm_cost."""
        assert estimate_openai_cost is estimate_llm_cost

    def test_estimate_llm_cost_uses_configured_rates(self):
        """Cost estimation should use LLM_INPUT/OUTPUT_COST_PER_TOKEN constants."""
        input_tokens = 100
        output_tokens = 200

        cost = estimate_llm_cost(input_tokens, output_tokens)

        expected = 100 * LLM_INPUT_COST_PER_TOKEN + 200 * LLM_OUTPUT_COST_PER_TOKEN
        assert cost == round(expected, 6)

    def test_estimate_llm_cost_with_zero_tokens(self):
        """Cost should be zero when no tokens are used."""
        cost = estimate_llm_cost(0, 0)
        assert cost == 0.0

    def test_backward_compat_aliases(self):
        """GPT4O_MINI_* constants should match LLM_* constants."""
        assert GPT4O_MINI_INPUT_COST_PER_TOKEN == LLM_INPUT_COST_PER_TOKEN
        assert GPT4O_MINI_OUTPUT_COST_PER_TOKEN == LLM_OUTPUT_COST_PER_TOKEN

    def test_estimate_replicate_cost_calculates_correctly(self):
        """Replicate cost should use REPLICATE_COST_PER_IMAGE per image."""
        image_count = 10

        cost = estimate_replicate_cost(image_count)

        assert cost == round(10 * REPLICATE_COST_PER_IMAGE, 4)

    def test_estimate_replicate_cost_with_zero_images(self):
        """Cost should be zero when no images are generated."""
        cost = estimate_replicate_cost(0)
        assert cost == 0.0

    def test_estimate_replicate_cost_rounds_correctly(self):
        """Cost should be rounded to 4 decimal places."""
        cost = estimate_replicate_cost(3)
        assert cost == round(3 * REPLICATE_COST_PER_IMAGE, 4)


class TestSpendTracker:
    """Tests for the SpendTracker class."""

    def test_get_daily_spend_sums_costs_for_today(self):
        """get_daily_spend should sum ai_cost_estimate_usd for the current day."""
        mock_db = MagicMock()
        tracker = SpendTracker(db=mock_db)

        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        mock_db.select.return_value = [
            {"started_at": f"{today_str}T10:00:00Z", "ai_cost_estimate_usd": 1.5},
            {"started_at": f"{today_str}T14:00:00Z", "ai_cost_estimate_usd": 2.3},
            {"started_at": "2025-01-01T10:00:00Z", "ai_cost_estimate_usd": 5.0},  # Different day
        ]

        spend = tracker.get_daily_spend()

        assert spend == 3.8  # 1.5 + 2.3

    def test_get_daily_spend_returns_zero_when_no_data(self):
        """get_daily_spend should return 0.0 when no pipeline runs exist."""
        mock_db = MagicMock()
        tracker = SpendTracker(db=mock_db)

        mock_db.select.return_value = []

        spend = tracker.get_daily_spend()

        assert spend == 0.0

    def test_get_daily_spend_handles_null_costs(self):
        """get_daily_spend should skip rows with NULL ai_cost_estimate_usd."""
        mock_db = MagicMock()
        tracker = SpendTracker(db=mock_db)

        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        mock_db.select.return_value = [
            {"started_at": f"{today_str}T10:00:00Z", "ai_cost_estimate_usd": 1.5},
            {"started_at": f"{today_str}T14:00:00Z", "ai_cost_estimate_usd": None},
        ]

        spend = tracker.get_daily_spend()

        assert spend == 1.5

    def test_get_daily_spend_handles_database_error(self):
        """get_daily_spend should return 0.0 on database error."""
        mock_db = MagicMock()
        tracker = SpendTracker(db=mock_db)

        mock_db.select.side_effect = DatabaseError("Connection failed")

        spend = tracker.get_daily_spend()

        assert spend == 0.0

    def test_get_monthly_spend_sums_costs_for_current_month(self):
        """get_monthly_spend should sum ai_cost_estimate_usd for the current month."""
        mock_db = MagicMock()
        tracker = SpendTracker(db=mock_db)

        now = datetime.now(timezone.utc)
        current_month = f"{now.year}-{now.month:02d}"

        mock_db.select.return_value = [
            {"started_at": f"{current_month}-05T10:00:00Z", "ai_cost_estimate_usd": 10.5},
            {"started_at": f"{current_month}-10T14:00:00Z", "ai_cost_estimate_usd": 20.3},
            {"started_at": "2024-12-01T10:00:00Z", "ai_cost_estimate_usd": 50.0},  # Different month
        ]

        spend = tracker.get_monthly_spend()

        assert spend == 30.8  # 10.5 + 20.3

    def test_get_monthly_spend_accepts_specific_year_and_month(self):
        """get_monthly_spend should accept year and month parameters."""
        mock_db = MagicMock()
        tracker = SpendTracker(db=mock_db)

        mock_db.select.return_value = [
            {"started_at": "2024-06-15T10:00:00Z", "ai_cost_estimate_usd": 15.0},
            {"started_at": "2024-06-20T14:00:00Z", "ai_cost_estimate_usd": 25.0},
            {"started_at": "2024-07-01T10:00:00Z", "ai_cost_estimate_usd": 10.0},
        ]

        spend = tracker.get_monthly_spend(year=2024, month=6)

        assert spend == 40.0  # 15.0 + 25.0

    def test_get_monthly_spend_returns_zero_when_no_data(self):
        """get_monthly_spend should return 0.0 when no pipeline runs exist."""
        mock_db = MagicMock()
        tracker = SpendTracker(db=mock_db)

        mock_db.select.return_value = []

        spend = tracker.get_monthly_spend()

        assert spend == 0.0

    def test_check_budget_returns_can_proceed_true_when_under_cap(self):
        """check_budget should allow operations when under the hard stop cap."""
        mock_db = MagicMock()
        mock_alerter = MagicMock()
        tracker = SpendTracker(db=mock_db, alerter=mock_alerter, monthly_cap=150.0)

        mock_db.select.return_value = [
            {"started_at": "2026-02-05T10:00:00Z", "ai_cost_estimate_usd": 50.0},
        ]

        result = tracker.check_budget()

        assert result["can_proceed"] is True
        assert result["monthly_spend"] == 50.0
        assert result["warning"] is False
        assert result["hard_stop"] is False
        mock_alerter.send_budget_warning.assert_not_called()

    def test_check_budget_sends_warning_alert_at_120_threshold(self):
        """check_budget should send alert when spend reaches $120."""
        mock_db = MagicMock()
        mock_alerter = MagicMock()
        tracker = SpendTracker(db=mock_db, alerter=mock_alerter, monthly_warning=120.0, monthly_cap=150.0)

        mock_db.select.return_value = [
            {"started_at": "2026-02-05T10:00:00Z", "ai_cost_estimate_usd": 125.0},
        ]

        result = tracker.check_budget()

        assert result["can_proceed"] is True
        assert result["monthly_spend"] == 125.0
        assert result["warning"] is True
        assert result["hard_stop"] is False
        mock_alerter.send_budget_warning.assert_called_once_with(125.0, 150.0)

    def test_check_budget_sends_hard_stop_alert_at_150_threshold(self):
        """check_budget should send alert and halt operations when spend reaches $150."""
        mock_db = MagicMock()
        mock_alerter = MagicMock()
        tracker = SpendTracker(db=mock_db, alerter=mock_alerter, monthly_warning=120.0, monthly_cap=150.0)

        mock_db.select.return_value = [
            {"started_at": "2026-02-05T10:00:00Z", "ai_cost_estimate_usd": 150.0},
        ]

        result = tracker.check_budget()

        assert result["can_proceed"] is False
        assert result["monthly_spend"] == 150.0
        assert result["warning"] is True
        assert result["hard_stop"] is True
        mock_alerter.send_budget_warning.assert_called_once_with(150.0, 150.0)

    def test_check_budget_only_sends_alert_once_per_month(self):
        """check_budget should only send alert once per month."""
        mock_db = MagicMock()
        mock_alerter = MagicMock()
        tracker = SpendTracker(db=mock_db, alerter=mock_alerter, monthly_warning=120.0, monthly_cap=150.0)

        mock_db.select.return_value = [
            {"started_at": "2026-02-05T10:00:00Z", "ai_cost_estimate_usd": 125.0},
        ]

        # First call should send alert
        tracker.check_budget()
        assert mock_alerter.send_budget_warning.call_count == 1

        # Second call should NOT send alert (already sent this month)
        tracker.check_budget()
        assert mock_alerter.send_budget_warning.call_count == 1

    def test_check_budget_handles_alerter_failure_gracefully(self):
        """check_budget should not crash if alert sending fails."""
        mock_db = MagicMock()
        mock_alerter = MagicMock()
        tracker = SpendTracker(db=mock_db, alerter=mock_alerter, monthly_warning=120.0)

        mock_db.select.return_value = [
            {"started_at": "2026-02-05T10:00:00Z", "ai_cost_estimate_usd": 125.0},
        ]
        mock_alerter.send_budget_warning.side_effect = Exception("Email failed")

        result = tracker.check_budget()

        # Should still return correct status despite alert failure
        assert result["warning"] is True
        assert result["monthly_spend"] == 125.0

    def test_check_daily_budget_returns_warning_false_when_under_threshold(self):
        """check_daily_budget should return warning=False when under $8."""
        mock_db = MagicMock()
        mock_alerter = MagicMock()
        tracker = SpendTracker(db=mock_db, alerter=mock_alerter, daily_warning=8.0)

        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        mock_db.select.return_value = [
            {"started_at": f"{today_str}T10:00:00Z", "ai_cost_estimate_usd": 5.0},
        ]

        result = tracker.check_daily_budget()

        assert result["warning"] is False
        assert result["daily_spend"] == 5.0
        mock_alerter.send_alert.assert_not_called()

    def test_check_daily_budget_sends_alert_when_exceeding_8_threshold(self):
        """check_daily_budget should send alert when daily spend exceeds $8."""
        mock_db = MagicMock()
        mock_alerter = MagicMock()
        tracker = SpendTracker(db=mock_db, alerter=mock_alerter, daily_warning=8.0)

        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        mock_db.select.return_value = [
            {"started_at": f"{today_str}T10:00:00Z", "ai_cost_estimate_usd": 9.5},
        ]

        result = tracker.check_daily_budget()

        assert result["warning"] is True
        assert result["daily_spend"] == 9.5
        mock_alerter.send_alert.assert_called_once()

        # Verify alert content
        call_args = mock_alerter.send_alert.call_args
        assert "Daily AI spend warning" in call_args[0][0]
        assert "$9.50" in call_args[0][1] or "$9.5" in call_args[0][1]
        assert call_args[1]["level"] == "warning"

    def test_check_daily_budget_handles_alerter_failure_gracefully(self):
        """check_daily_budget should not crash if alert sending fails."""
        mock_db = MagicMock()
        mock_alerter = MagicMock()
        tracker = SpendTracker(db=mock_db, alerter=mock_alerter, daily_warning=8.0)

        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        mock_db.select.return_value = [
            {"started_at": f"{today_str}T10:00:00Z", "ai_cost_estimate_usd": 10.0},
        ]
        mock_alerter.send_alert.side_effect = Exception("Email failed")

        result = tracker.check_daily_budget()

        # Should still return correct status despite alert failure
        assert result["warning"] is True
        assert result["daily_spend"] == 10.0

    def test_spend_tracker_uses_custom_thresholds(self):
        """SpendTracker should accept custom threshold values."""
        mock_db = MagicMock()
        tracker = SpendTracker(
            db=mock_db,
            monthly_warning=100.0,
            monthly_cap=130.0,
            daily_warning=5.0,
        )

        mock_db.select.return_value = [
            {"started_at": "2026-02-05T10:00:00Z", "ai_cost_estimate_usd": 105.0},
        ]

        result = tracker.check_budget()

        # Should trigger warning at $100, not default $120
        assert result["warning"] is True
        assert result["monthly_spend"] == 105.0
        assert result["can_proceed"] is True  # Still under $130 cap

    def test_check_budget_message_content(self):
        """check_budget should return appropriate messages for each state."""
        mock_db = MagicMock()
        mock_alerter = MagicMock()
        tracker = SpendTracker(db=mock_db, alerter=mock_alerter, monthly_warning=120.0, monthly_cap=150.0)

        # Under warning
        mock_db.select.return_value = [{"started_at": "2026-02-05T10:00:00Z", "ai_cost_estimate_usd": 50.0}]
        result = tracker.check_budget()
        assert "Monthly AI spend: $50.00 / $150.00" == result["message"]

        # At warning
        mock_db.select.return_value = [{"started_at": "2026-02-05T10:00:00Z", "ai_cost_estimate_usd": 120.0}]
        result = tracker.check_budget()
        assert "WARNING" in result["message"]
        assert "$120.00" in result["message"]

        # Reset alert flag for next test
        tracker._alert_sent_for_month = None

        # At hard stop
        mock_db.select.return_value = [{"started_at": "2026-02-05T10:00:00Z", "ai_cost_estimate_usd": 150.0}]
        result = tracker.check_budget()
        assert "HARD STOP" in result["message"]
        assert "halted" in result["message"]
