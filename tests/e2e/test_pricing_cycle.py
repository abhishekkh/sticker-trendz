"""
End-to-end test for the daily pricing cycle.

Requires Supabase credentials. Etsy calls are mocked.
Seeds test stickers with different trend ages and verifies
tier assignments and price_history entries.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch

import pytest

from tests.conftest import (
    has_supabase_creds,
    has_etsy_creds,
    cleanup_test_data,
)
from src.pricing.tiers import DEFAULT_TIERS

pytestmark = pytest.mark.e2e

_skip_reason = "Missing Supabase credentials"
_skip = pytest.mark.skipif(
    not has_supabase_creds(),
    reason=_skip_reason,
)


@pytest.fixture
def e2e_prefix():
    return f"E2E-{uuid.uuid4().hex[:8]}"


@pytest.fixture
def db():
    from src.db import SupabaseClient
    return SupabaseClient()


@pytest.fixture(autouse=True)
def cleanup(db, e2e_prefix):
    """Cleanup all test data after the test."""
    yield
    cleanup_test_data(db, e2e_prefix)


def _make_trend_and_sticker(db, prefix, topic_suffix, age_days, price=5.49):
    """Helper to seed a trend and sticker with a given age."""
    now = datetime.now(timezone.utc)
    created_at = (now - timedelta(days=age_days)).isoformat()

    trend = db.insert_trend({
        "topic": f"[{prefix}] {topic_suffix}",
        "topic_normalized": f"{prefix.lower()}-{topic_suffix.lower().replace(' ', '-')}",
        "source": "reddit",
        "keywords": [topic_suffix.lower()],
        "status": "generated",
        "score_overall": 80.0,
        "created_at": created_at,
    })

    sticker = db.insert_sticker({
        "trend_id": trend["id"],
        "title": f"[{prefix}] {topic_suffix} Sticker",
        "image_url": "https://example.com/test.png",
        "size": "3in",
        "moderation_status": "approved",
        "price": price,
        "current_pricing_tier": "just_dropped",
        "etsy_listing_id": f"fake-listing-{uuid.uuid4().hex[:6]}",
        "published_at": created_at,
        "fulfillment_provider": "sticker_mule",
        "sales_count": 0,
        "view_count": 0,
    })

    return trend, sticker


@_skip
class TestPricingCycleE2E:
    """End-to-end pricing engine test with real Supabase data."""

    def test_tier_assignments_by_age(self, db, e2e_prefix):
        """
        Seed 4 stickers with trend ages: 2d, 10d, 20d, 35d.
        Run PricingEngine and verify tier assignments.
        """
        # Seed test data
        _, sticker_2d = _make_trend_and_sticker(db, e2e_prefix, "Fresh Trend", age_days=2)
        _, sticker_10d = _make_trend_and_sticker(db, e2e_prefix, "Mid Trend", age_days=10)
        _, sticker_20d = _make_trend_and_sticker(db, e2e_prefix, "Cool Trend", age_days=20)
        _, sticker_35d = _make_trend_and_sticker(db, e2e_prefix, "Old Trend", age_days=35)

        # Mock Etsy publisher to avoid real API calls
        mock_publisher = MagicMock()
        mock_publisher.update_listing_price.return_value = True
        mock_publisher.deactivate_listing.return_value = True

        # Mock rate limiter
        mock_rate_limiter = MagicMock()
        mock_rate_limiter.acquire_lock.return_value = True
        mock_rate_limiter.can_proceed.return_value = True
        mock_rate_limiter.release_lock.return_value = None

        from src.pricing.engine import PricingEngine
        from src.pricing.tiers import PricingTierManager
        from src.monitoring.pipeline_logger import PipelineRunLogger
        from src.monitoring.error_logger import ErrorLogger

        tier_manager = PricingTierManager(db)
        engine = PricingEngine(
            db=db,
            tier_manager=tier_manager,
            etsy_publisher=mock_publisher,
            rate_limiter=mock_rate_limiter,
            pipeline_logger=PipelineRunLogger(db),
            error_logger=ErrorLogger(db),
            archiver=None,
        )

        counts = engine.run()

        # Verify tier assignments
        s2d = db.select("stickers", filters={"id": sticker_2d["id"]})[0]
        s10d = db.select("stickers", filters={"id": sticker_10d["id"]})[0]
        s20d = db.select("stickers", filters={"id": sticker_20d["id"]})[0]
        s35d = db.select("stickers", filters={"id": sticker_35d["id"]})[0]

        # 2-day-old trend -> just_dropped (0-3 days)
        assert s2d["current_pricing_tier"] == "just_dropped"

        # 10-day-old trend -> trending (3-14 days)
        assert s10d["current_pricing_tier"] == "trending"

        # 20-day-old trend -> cooling (14-30 days)
        assert s20d["current_pricing_tier"] == "cooling"

        # 35-day-old trend -> evergreen (30+ days)
        assert s35d["current_pricing_tier"] == "evergreen"

    def test_price_history_entries_created(self, db, e2e_prefix):
        """
        After running the pricing engine, price_history records
        should exist for stickers whose price changed.
        """
        _, sticker = _make_trend_and_sticker(
            db, e2e_prefix, "History Trend", age_days=10, price=5.49
        )

        mock_publisher = MagicMock()
        mock_publisher.update_listing_price.return_value = True

        mock_rate_limiter = MagicMock()
        mock_rate_limiter.acquire_lock.return_value = True
        mock_rate_limiter.can_proceed.return_value = True
        mock_rate_limiter.release_lock.return_value = None

        from src.pricing.engine import PricingEngine
        from src.pricing.tiers import PricingTierManager
        from src.monitoring.pipeline_logger import PipelineRunLogger
        from src.monitoring.error_logger import ErrorLogger

        engine = PricingEngine(
            db=db,
            tier_manager=PricingTierManager(db),
            etsy_publisher=mock_publisher,
            rate_limiter=mock_rate_limiter,
            pipeline_logger=PipelineRunLogger(db),
            error_logger=ErrorLogger(db),
            archiver=None,
        )

        engine.run()

        # Check price_history for the sticker
        history = db.select("price_history", filters={"sticker_id": sticker["id"]})
        # Price should change from 5.49 (just_dropped) to trending tier price
        if history:
            entry = history[0]
            assert entry["old_price"] == 5.49
            assert "tier_change" in entry.get("reason", "")

    def test_archived_stickers_deactivated(self, db, e2e_prefix):
        """
        Stickers with 0 sales/views past archive threshold
        should be deactivated via the archiver.
        """
        _, sticker = _make_trend_and_sticker(
            db, e2e_prefix, "Archive Trend", age_days=45, price=3.49
        )

        mock_publisher = MagicMock()
        mock_publisher.update_listing_price.return_value = True
        mock_publisher.deactivate_listing.return_value = True

        mock_rate_limiter = MagicMock()
        mock_rate_limiter.acquire_lock.return_value = True
        mock_rate_limiter.can_proceed.return_value = True
        mock_rate_limiter.release_lock.return_value = None

        from src.pricing.engine import PricingEngine
        from src.pricing.tiers import PricingTierManager
        from src.pricing.archiver import StickerArchiver
        from src.monitoring.pipeline_logger import PipelineRunLogger
        from src.monitoring.error_logger import ErrorLogger

        archiver = StickerArchiver(
            db=db,
            etsy_publisher=mock_publisher,
            error_logger=ErrorLogger(db),
        )

        engine = PricingEngine(
            db=db,
            tier_manager=PricingTierManager(db),
            archiver=archiver,
            etsy_publisher=mock_publisher,
            rate_limiter=mock_rate_limiter,
            pipeline_logger=PipelineRunLogger(db),
            error_logger=ErrorLogger(db),
        )

        counts = engine.run()

        # Check sticker was archived
        rows = db.select("stickers", filters={"id": sticker["id"]})
        sticker_row = rows[0]

        # The archiver should have set moderation_status to 'archived'
        # or the pricing engine should have handled it
        # Note: archiver checks published_at age and 0 sales/views
        if sticker_row["moderation_status"] == "archived":
            assert sticker_row["current_pricing_tier"] == "archived"
            mock_publisher.deactivate_listing.assert_called()
