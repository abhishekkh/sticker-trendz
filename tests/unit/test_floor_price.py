"""Tests for src/pricing/tiers.py -- floor price, rounding, tier assignment, price lookups."""

from unittest.mock import MagicMock

import pytest

from src.pricing.tiers import (
    round_to_price_point,
    calculate_floor_price,
    PricingTierManager,
    DEFAULT_TIERS,
    DEFAULT_ETSY_FEE_RATE,
    DEFAULT_MIN_MARGIN,
)
from src.db import DatabaseError


class TestRoundToPricePoint:
    """Test price rounding to .49 or .99."""

    def test_price_rounds_to_nearest_49_or_99_above_input(self):
        """Price is rounded to smallest .49 or .99 that is >= input."""
        assert round_to_price_point(3.00) == 3.49
        assert round_to_price_point(3.49) == 3.49
        assert round_to_price_point(3.50) == 3.99
        assert round_to_price_point(3.72) == 3.99
        assert round_to_price_point(3.99) == 3.99
        assert round_to_price_point(4.00) == 4.49

    def test_price_below_one_returns_49(self):
        """Price <= 0 returns 0.49."""
        assert round_to_price_point(0) == 0.49
        assert round_to_price_point(-1.0) == 0.49
        assert round_to_price_point(0.10) == 0.49

    def test_high_price_rounds_correctly(self):
        """Larger prices round to .49 or .99."""
        assert round_to_price_point(5.49) == 5.49
        assert round_to_price_point(5.99) == 5.99
        assert round_to_price_point(6.25) == 6.49
        assert round_to_price_point(6.75) == 6.99


class TestCalculateFloorPrice:
    """Test floor price calculation: (costs) / (1 - fee_rate) / (1 - margin)."""

    def test_floor_price_formula_with_known_inputs(self):
        """Floor = (print + shipping + packaging) / (1 - fee) / (1 - margin)."""
        # $1.50 + $0.93 + $0.15 = $2.58; 2.58 / 0.9 / 0.8 = 3.583...
        floor = calculate_floor_price(
            print_cost=1.50,
            shipping_cost=0.93,
            packaging_cost=0.15,
        )
        assert abs(floor - 3.58) < 0.02

    def test_floor_price_with_custom_fee_and_margin(self):
        """Custom etsy_fee_rate and min_margin are applied."""
        # 2.00 / (1 - 0.10) / (1 - 0.20) = 2 / 0.9 / 0.8 = 2.777...
        floor = calculate_floor_price(
            print_cost=1.0,
            shipping_cost=0.5,
            packaging_cost=0.5,
            etsy_fee_rate=0.10,
            min_margin=0.20,
        )
        assert abs(floor - 2.78) < 0.02

    def test_floor_price_zero_costs(self):
        """Zero costs yield zero floor."""
        floor = calculate_floor_price(0, 0, 0)
        assert floor == 0.0

    def test_floor_price_uses_default_fee_and_margin(self):
        """Defaults: fee 10%, margin 20%."""
        floor = calculate_floor_price(1.0, 0.5, 0.5)
        # 2.0 / 0.9 / 0.8 = 2.777...
        assert floor > 2.5 and floor < 3.0


class TestPricingTierManagerTierAssignment:
    """Test tier assignment by trend age: 2 days=just_dropped, 10=trending, 20=cooling, 35=evergreen."""

    def test_tier_2_days_just_dropped(self):
        """2 days since trend -> just_dropped."""
        mock_db = MagicMock()
        mock_db.get_pricing_tiers.return_value = DEFAULT_TIERS
        manager = PricingTierManager(db=mock_db)
        assert manager.get_tier_for_age(0) == "just_dropped"
        assert manager.get_tier_for_age(1) == "just_dropped"
        assert manager.get_tier_for_age(2) == "just_dropped"

    def test_tier_10_days_trending(self):
        """10 days since trend -> trending."""
        mock_db = MagicMock()
        mock_db.get_pricing_tiers.return_value = DEFAULT_TIERS
        manager = PricingTierManager(db=mock_db)
        assert manager.get_tier_for_age(3) == "trending"
        assert manager.get_tier_for_age(10) == "trending"
        assert manager.get_tier_for_age(13) == "trending"

    def test_tier_20_days_cooling(self):
        """20 days since trend -> cooling."""
        mock_db = MagicMock()
        mock_db.get_pricing_tiers.return_value = DEFAULT_TIERS
        manager = PricingTierManager(db=mock_db)
        assert manager.get_tier_for_age(14) == "cooling"
        assert manager.get_tier_for_age(20) == "cooling"
        assert manager.get_tier_for_age(29) == "cooling"

    def test_tier_35_days_evergreen(self):
        """35 days since trend -> evergreen."""
        mock_db = MagicMock()
        mock_db.get_pricing_tiers.return_value = DEFAULT_TIERS
        manager = PricingTierManager(db=mock_db)
        assert manager.get_tier_for_age(30) == "evergreen"
        assert manager.get_tier_for_age(35) == "evergreen"
        assert manager.get_tier_for_age(100) == "evergreen"

    def test_tier_fallback_when_db_fails(self):
        """When DB fails, default tiers are used."""
        mock_db = MagicMock()
        mock_db.get_pricing_tiers.side_effect = DatabaseError("Connection failed")
        manager = PricingTierManager(db=mock_db)
        assert manager.get_tier_for_age(2) == "just_dropped"
        assert manager.get_tier_for_age(10) == "trending"
        assert manager.get_tier_for_age(20) == "cooling"
        assert manager.get_tier_for_age(35) == "evergreen"


class TestPricingTierManagerPriceLookups:
    """Test correct prices for each tier and product type."""

    def test_just_dropped_prices(self):
        """just_dropped: single_small 5.49, single_large 6.49."""
        mock_db = MagicMock()
        mock_db.get_pricing_tiers.return_value = DEFAULT_TIERS
        manager = PricingTierManager(db=mock_db)
        assert manager.get_price("just_dropped", "single_small") == 5.49
        assert manager.get_price("just_dropped", "single_large") == 6.49

    def test_trending_prices(self):
        """trending: single_small 4.49, single_large 5.49."""
        mock_db = MagicMock()
        mock_db.get_pricing_tiers.return_value = DEFAULT_TIERS
        manager = PricingTierManager(db=mock_db)
        assert manager.get_price("trending", "single_small") == 4.49
        assert manager.get_price("trending", "single_large") == 5.49

    def test_cooling_and_evergreen_prices(self):
        """cooling and evergreen: single_small 3.49, single_large 4.49."""
        mock_db = MagicMock()
        mock_db.get_pricing_tiers.return_value = DEFAULT_TIERS
        manager = PricingTierManager(db=mock_db)
        for tier in ("cooling", "evergreen"):
            assert manager.get_price(tier, "single_small") == 3.49
            assert manager.get_price(tier, "single_large") == 4.49

    def test_unknown_tier_uses_evergreen_fallback(self):
        """Unknown tier falls back to evergreen prices."""
        mock_db = MagicMock()
        mock_db.get_pricing_tiers.return_value = DEFAULT_TIERS
        manager = PricingTierManager(db=mock_db)
        assert manager.get_price("unknown_tier", "single_small") == 3.49
        assert manager.get_price("unknown_tier", "single_large") == 4.49


class TestPricingTierManagerFloorPrice:
    """Test get_floor_price uses shipping rates and round_to_price_point."""

    def test_floor_price_uses_shipping_rates_from_db(self):
        """Floor is calculated from print + shipping + packaging, then rounded."""
        mock_db = MagicMock()
        mock_db.get_pricing_tiers.return_value = DEFAULT_TIERS
        mock_db.get_shipping_rate.return_value = {
            "shipping_cost": 0.93,
            "packaging_cost": 0.15,
        }
        manager = PricingTierManager(db=mock_db)
        floor = manager.get_floor_price(
            product_type="single_small",
            fulfillment_provider="sticker_mule",
            print_cost=1.50,
        )
        # Raw floor ~3.58, rounded to .49/.99 -> 3.99
        assert floor >= 3.49
        assert floor <= 4.49
        assert floor in (3.49, 3.99, 4.49)

    def test_floor_price_single_large_uses_higher_print_cost(self):
        """single_large defaults print_cost to 2.00 when 1.50 passed."""
        mock_db = MagicMock()
        mock_db.get_pricing_tiers.return_value = DEFAULT_TIERS
        mock_db.get_shipping_rate.return_value = {
            "shipping_cost": 1.0,
            "packaging_cost": 0.20,
        }
        manager = PricingTierManager(db=mock_db)
        floor = manager.get_floor_price(
            product_type="single_large",
            fulfillment_provider="sticker_mule",
            print_cost=1.50,  # should become 2.00 for single_large
        )
        # 2 + 1 + 0.2 = 3.2; 3.2/0.9/0.8 = 4.44 -> 4.49
        assert floor >= 4.0

    def test_floor_price_db_error_uses_fallback_costs(self):
        """When get_shipping_rate fails, fallback costs are used (self_usps)."""
        mock_db = MagicMock()
        mock_db.get_pricing_tiers.return_value = DEFAULT_TIERS
        mock_db.get_shipping_rate.side_effect = DatabaseError("DB error")
        manager = PricingTierManager(db=mock_db)
        floor = manager.get_floor_price(
            product_type="single_small",
            fulfillment_provider="self_usps",
        )
        # Fallback: shipping 0.78, packaging 0.15; print 1.50 -> 2.43/0.9/0.8 ~ 3.38 -> 3.49
        assert floor >= 3.0
        assert floor <= 4.0
