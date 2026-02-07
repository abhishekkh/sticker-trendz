"""Tests for src/pricing/engine.py and src/pricing/archiver.py -- pricing engine and archival."""

from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch

import pytest

from src.pricing.engine import (
    PricingEngine,
    SALES_OVERRIDE_THRESHOLD,
    WORKFLOW_NAME,
)
from src.pricing.archiver import (
    StickerArchiver,
    ARCHIVE_THRESHOLD_DAYS,
)
from src.pricing.tiers import PricingTierManager, DEFAULT_TIERS, round_to_price_point
from src.db import DatabaseError


class TestPricingEngineCalculateTrendAge:
    """Test _calculate_trend_age."""

    def test_trend_age_from_created_at(self):
        """Trend age is days since created_at."""
        engine = PricingEngine(db=MagicMock(), tier_manager=MagicMock())
        now = datetime.now(timezone.utc)
        two_days_ago = (now - timedelta(days=2)).isoformat().replace("+00:00", "Z")
        age = engine._calculate_trend_age(two_days_ago)
        assert age == 2

    def test_trend_age_zero_for_invalid_date(self):
        """Invalid or empty created_at returns 0."""
        engine = PricingEngine(db=MagicMock(), tier_manager=MagicMock())
        assert engine._calculate_trend_age("") == 0
        assert engine._calculate_trend_age("not-a-date") == 0


class TestPricingEngineSalesOverride:
    """Test sales override: 10+ sales at current tier keeps price unchanged."""

    def test_sales_override_10_plus_sales_at_current_tier_returns_true(self):
        """10+ orders at current tier -> override applies (keep price)."""
        mock_db = MagicMock()
        mock_db.select.return_value = [{"id": f"o{i}"} for i in range(10)]
        engine = PricingEngine(db=mock_db, tier_manager=MagicMock())
        result = engine._check_sales_override("sticker-1", "trending")
        assert result is True

    def test_sales_override_9_sales_returns_false(self):
        """9 sales at current tier -> no override."""
        mock_db = MagicMock()
        mock_db.select.return_value = [{"id": f"o{i}"} for i in range(9)]
        engine = PricingEngine(db=mock_db, tier_manager=MagicMock())
        result = engine._check_sales_override("sticker-1", "trending")
        assert result is False

    def test_sales_override_counter_resets_when_tier_changes(self):
        """Override is per current_tier: 10+ sales at 'trending' don't count for 'cooling'."""
        mock_db = MagicMock()
        # 12 orders at tier "trending"
        mock_db.select.return_value = [{"id": f"o{i}"} for i in range(12)]
        engine = PricingEngine(db=mock_db, tier_manager=MagicMock())
        # Override applies when current_tier is "trending"
        assert engine._check_sales_override("sticker-1", "trending") is True
        # When we check for "cooling", we filter by pricing_tier_at_sale == "cooling" -> 0 orders
        mock_db.select.return_value = []
        assert engine._check_sales_override("sticker-1", "cooling") is False

    def test_sales_override_db_error_returns_false(self):
        """Database error -> no override."""
        mock_db = MagicMock()
        mock_db.select.side_effect = DatabaseError("DB error")
        engine = PricingEngine(db=mock_db, tier_manager=MagicMock())
        assert engine._check_sales_override("sticker-1", "trending") is False


class TestPricingEngineHasRecentSales:
    """Test _has_recent_sales."""

    def test_has_recent_sales_true_when_last_sale_within_14_days(self):
        """last_sale_at within 14 days -> True."""
        mock_db = MagicMock()
        five_days_ago = (datetime.now(timezone.utc) - timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
        mock_db.select.side_effect = [
            [{"id": "o1"}],
            [{"last_sale_at": five_days_ago}],
        ]
        engine = PricingEngine(db=mock_db, tier_manager=MagicMock())
        assert engine._has_recent_sales("sticker-1", days=14) is True

    def test_has_recent_sales_false_when_last_sale_over_14_days_ago(self):
        """last_sale_at older than 14 days -> False."""
        mock_db = MagicMock()
        twenty_days_ago = (datetime.now(timezone.utc) - timedelta(days=20)).strftime("%Y-%m-%dT%H:%M:%SZ")
        mock_db.select.side_effect = [
            [{"id": "o1"}],
            [{"last_sale_at": twenty_days_ago}],
        ]
        engine = PricingEngine(db=mock_db, tier_manager=MagicMock())
        assert engine._has_recent_sales("sticker-1", days=14) is False

    def test_has_recent_sales_false_when_no_orders(self):
        """No orders -> False."""
        mock_db = MagicMock()
        mock_db.select.return_value = []
        engine = PricingEngine(db=mock_db, tier_manager=MagicMock())
        assert engine._has_recent_sales("sticker-1") is False


class TestPricingEngineProcessSticker:
    """Test _process_sticker: floor enforcement, price rounding, tier assignment."""

    def test_price_never_goes_below_floor(self):
        """When tier price is below floor, floor price is used."""
        mock_db = MagicMock()
        mock_db.select.return_value = []  # no sales override
        mock_tiers = MagicMock(spec=PricingTierManager)
        mock_tiers.get_tier_for_age.return_value = "cooling"
        mock_tiers.get_price.return_value = 2.50  # below typical floor
        mock_tiers.get_floor_price.return_value = 3.49
        engine = PricingEngine(db=mock_db, tier_manager=mock_tiers, etsy_publisher=None)
        sticker = {
            "id": "s1",
            "etsy_listing_id": "",
            "price": 4.49,
            "current_pricing_tier": "trending",
            "size": "3in",
            "fulfillment_provider": "sticker_mule",
            "trends": {"created_at": (datetime.now(timezone.utc) - timedelta(days=20)).isoformat() + "Z"},
            "created_at": (datetime.now(timezone.utc) - timedelta(days=20)).isoformat() + "Z",
            "sales_count": 0,
        }
        updated = engine._process_sticker(sticker)
        mock_tiers.get_floor_price.assert_called()
        call_kw = mock_db.update_sticker.call_args[0][1]
        assert call_kw["price"] == 3.49
        assert call_kw["price"] >= mock_tiers.get_floor_price.return_value

    def test_sales_override_keeps_price_unchanged(self):
        """When sales override applies, price is not updated (returns False)."""
        mock_db = MagicMock()
        mock_db.select.return_value = [{"id": f"o{i}"} for i in range(10)]
        mock_tiers = MagicMock(spec=PricingTierManager)
        mock_tiers.get_tier_for_age.return_value = "trending"
        mock_tiers.get_price.return_value = 4.49
        mock_tiers.get_floor_price.return_value = 3.49
        engine = PricingEngine(
            db=mock_db,
            tier_manager=mock_tiers,
            etsy_publisher=None,
        )
        sticker = {
            "id": "s1",
            "etsy_listing_id": "",
            "price": 4.49,
            "current_pricing_tier": "trending",
            "size": "3in",
            "fulfillment_provider": "sticker_mule",
            "trends": {"created_at": (datetime.now(timezone.utc) - timedelta(days=10)).isoformat() + "Z"},
            "created_at": (datetime.now(timezone.utc) - timedelta(days=10)).isoformat() + "Z",
            "sales_count": 12,
        }
        updated = engine._process_sticker(sticker)
        assert updated is False
        mock_tiers.get_price.assert_not_called()

    def test_new_price_rounded_to_49_or_99(self):
        """Final price passed to update is rounded via round_to_price_point."""
        mock_db = MagicMock()
        mock_tiers = MagicMock(spec=PricingTierManager)
        mock_tiers.get_tier_for_age.return_value = "cooling"
        mock_tiers.get_price.return_value = 3.72
        mock_tiers.get_floor_price.return_value = 3.49
        engine = PricingEngine(db=mock_db, tier_manager=mock_tiers, etsy_publisher=None)
        sticker = {
            "id": "s1",
            "etsy_listing_id": "",
            "price": 4.49,
            "current_pricing_tier": "trending",
            "size": "3in",
            "fulfillment_provider": "sticker_mule",
            "trends": {"created_at": (datetime.now(timezone.utc) - timedelta(days=20)).isoformat() + "Z"},
            "created_at": (datetime.now(timezone.utc) - timedelta(days=20)).isoformat() + "Z",
            "sales_count": 0,
        }
        engine._process_sticker(sticker)
        call_kw = mock_db.update_sticker.call_args[0][1]
        new_price = call_kw["price"]
        assert new_price in (3.49, 3.99, 4.49)
        assert new_price == round_to_price_point(new_price)


class TestStickerArchiverGetArchivableStickers:
    """Test archival criteria: 0 sales and 0 views for 14+ days triggers archive."""

    def test_archivable_0_sales_0_views_14_plus_days(self):
        """Sticker with 0 sales, 0 views, published 14+ days ago is archivable."""
        mock_db = MagicMock()
        cutoff = datetime.now(timezone.utc) - timedelta(days=ARCHIVE_THRESHOLD_DAYS)
        old_published = (cutoff - timedelta(days=1)).isoformat().replace("+00:00", "Z")
        mock_db.get_published_stickers.return_value = [
            {
                "id": "s1",
                "etsy_listing_id": "listing-1",
                "sales_count": 0,
                "view_count": 0,
                "published_at": old_published,
                "moderation_status": "approved",
            },
        ]
        archiver = StickerArchiver(db=mock_db)
        result = archiver.get_archivable_stickers()
        assert len(result) == 1
        assert result[0]["id"] == "s1"

    def test_not_archivable_when_recent_sales(self):
        """Sticker with recent sales is NOT archived (excluded from archivable list)."""
        mock_db = MagicMock()
        cutoff = datetime.now(timezone.utc) - timedelta(days=ARCHIVE_THRESHOLD_DAYS)
        old_published = (cutoff - timedelta(days=1)).isoformat().replace("+00:00", "Z")
        mock_db.get_published_stickers.return_value = [
            {
                "id": "s1",
                "etsy_listing_id": "listing-1",
                "sales_count": 1,
                "view_count": 0,
                "published_at": old_published,
                "moderation_status": "approved",
            },
        ]
        archiver = StickerArchiver(db=mock_db)
        result = archiver.get_archivable_stickers()
        assert len(result) == 0

    def test_not_archivable_when_has_views(self):
        """Sticker with views but 0 sales is NOT archivable (0 sales AND 0 views required)."""
        mock_db = MagicMock()
        cutoff = datetime.now(timezone.utc) - timedelta(days=ARCHIVE_THRESHOLD_DAYS)
        old_published = (cutoff - timedelta(days=1)).isoformat().replace("+00:00", "Z")
        mock_db.get_published_stickers.return_value = [
            {
                "id": "s1",
                "etsy_listing_id": "listing-1",
                "sales_count": 0,
                "view_count": 5,
                "published_at": old_published,
                "moderation_status": "approved",
            },
        ]
        archiver = StickerArchiver(db=mock_db)
        result = archiver.get_archivable_stickers()
        assert len(result) == 0

    def test_not_archivable_when_published_less_than_14_days_ago(self):
        """Sticker published 13 days ago is not yet archivable."""
        mock_db = MagicMock()
        recent = (datetime.now(timezone.utc) - timedelta(days=13)).isoformat().replace("+00:00", "Z")
        mock_db.get_published_stickers.return_value = [
            {
                "id": "s1",
                "etsy_listing_id": "listing-1",
                "sales_count": 0,
                "view_count": 0,
                "published_at": recent,
                "moderation_status": "approved",
            },
        ]
        archiver = StickerArchiver(db=mock_db)
        result = archiver.get_archivable_stickers()
        assert len(result) == 0

    def test_already_archived_skipped(self):
        """Stickers already archived are not in archivable list."""
        mock_db = MagicMock()
        cutoff = datetime.now(timezone.utc) - timedelta(days=ARCHIVE_THRESHOLD_DAYS)
        old_published = (cutoff - timedelta(days=1)).isoformat().replace("+00:00", "Z")
        mock_db.get_published_stickers.return_value = [
            {
                "id": "s1",
                "etsy_listing_id": "listing-1",
                "sales_count": 0,
                "view_count": 0,
                "published_at": old_published,
                "moderation_status": "archived",
            },
        ]
        archiver = StickerArchiver(db=mock_db)
        result = archiver.get_archivable_stickers()
        assert len(result) == 0


class TestStickerArchiverArchiveSticker:
    """Test archive_sticker updates status and logs to price_history."""

    def test_archive_sticker_deactivates_listing_and_updates_status(self):
        """archive_sticker deactivates Etsy listing and sets status to archived."""
        mock_db = MagicMock()
        mock_publisher = MagicMock()
        mock_publisher.deactivate_listing.return_value = True
        archiver = StickerArchiver(db=mock_db, etsy_publisher=mock_publisher)
        sticker = {
            "id": "s1",
            "etsy_listing_id": "listing-1",
            "price": 4.49,
        }
        result = archiver.archive_sticker(sticker)
        assert result is True
        mock_publisher.deactivate_listing.assert_called_once_with("listing-1")
        mock_db.update_sticker.assert_called_once()
        call_kw = mock_db.update_sticker.call_args[0][1]
        assert call_kw["moderation_status"] == "archived"
        assert call_kw["current_pricing_tier"] == "archived"
        mock_db.insert_price_history.assert_called_once()
        history = mock_db.insert_price_history.call_args[0][0]
        assert history["reason"] == "archived"
        assert history["pricing_tier"] == "archived"

    def test_archive_sticker_fails_when_deactivate_fails(self):
        """When deactivate_listing fails, archive_sticker returns False."""
        mock_db = MagicMock()
        mock_publisher = MagicMock()
        mock_publisher.deactivate_listing.return_value = False
        archiver = StickerArchiver(db=mock_db, etsy_publisher=mock_publisher)
        sticker = {"id": "s1", "etsy_listing_id": "listing-1", "price": 4.49}
        result = archiver.archive_sticker(sticker)
        assert result is False
        mock_db.update_sticker.assert_not_called()


class TestPricingEngineRun:
    """Test engine run with mocked dependencies."""

    def test_run_skips_when_lock_held(self):
        """When rate limiter lock is held, run exits without processing."""
        mock_db = MagicMock()
        mock_rate_limiter = MagicMock()
        mock_rate_limiter.acquire_lock.return_value = False
        mock_tiers = MagicMock(spec=PricingTierManager)
        engine = PricingEngine(
            db=mock_db,
            tier_manager=mock_tiers,
            rate_limiter=mock_rate_limiter,
            archiver=None,
            etsy_publisher=None,
        )
        counts = engine.run()
        assert counts["prices_updated"] == 0
        mock_db.get_published_stickers.assert_not_called()

    def test_run_skips_when_rate_limit_exceeded(self):
        """When can_proceed returns False, run skips price updates and does not fetch stickers."""
        mock_db = MagicMock()
        mock_rate_limiter = MagicMock()
        mock_rate_limiter.acquire_lock.return_value = True
        mock_rate_limiter.can_proceed.return_value = False
        mock_tiers = MagicMock(spec=PricingTierManager)
        engine = PricingEngine(
            db=mock_db,
            tier_manager=mock_tiers,
            rate_limiter=mock_rate_limiter,
            archiver=None,
            etsy_publisher=None,
        )
        counts = engine.run()
        assert counts["prices_updated"] == 0
        mock_db.get_published_stickers.assert_not_called()
