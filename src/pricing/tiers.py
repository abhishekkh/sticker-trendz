"""
Pricing tier configuration and lookups for Sticker Trendz.

Loads pricing tiers from the Supabase pricing_tiers table and provides
lookup functions for tier assignment by trend age and price retrieval.
Also calculates floor prices to ensure profitability.
"""

from __future__ import annotations

import logging
import math
from typing import Any, Dict, List, Optional

from src.db import SupabaseClient, DatabaseError

logger = logging.getLogger(__name__)

# Etsy fee rate (transaction + payment processing)
DEFAULT_ETSY_FEE_RATE: float = 0.10
# Minimum margin target
DEFAULT_MIN_MARGIN: float = 0.20

# Default tier definitions used if DB lookup fails
DEFAULT_TIERS: List[Dict[str, Any]] = [
    {"tier": "just_dropped", "min_trend_age_days": 0, "max_trend_age_days": 3,
     "price_single_small": 5.49, "price_single_large": 6.49},
    {"tier": "trending", "min_trend_age_days": 3, "max_trend_age_days": 14,
     "price_single_small": 4.49, "price_single_large": 5.49},
    {"tier": "cooling", "min_trend_age_days": 14, "max_trend_age_days": 30,
     "price_single_small": 3.49, "price_single_large": 4.49},
    {"tier": "evergreen", "min_trend_age_days": 30, "max_trend_age_days": None,
     "price_single_small": 3.49, "price_single_large": 4.49},
]


def round_to_price_point(price: float) -> float:
    """
    Round a price to the nearest .49 or .99 (psychological pricing).

    Rounds to whichever of .49 or .99 is closest without going below the
    input price. If the price is already at a valid point, it is returned
    as-is.

    Args:
        price: Raw price value.

    Returns:
        Price rounded to nearest .49 or .99.
    """
    if price <= 0:
        return 0.49

    base = math.floor(price)
    decimal = price - base

    # Determine the two candidate price points
    if decimal <= 0.49:
        candidate_low = base + 0.49
    else:
        candidate_low = base + 0.99

    # Pick the smallest valid candidate that is >= price
    candidates = [base + 0.49, base + 0.99, base + 1.49]
    for candidate in candidates:
        if candidate >= price:
            return candidate

    # Fallback: next dollar .49
    return base + 1.49


def calculate_floor_price(
    print_cost: float,
    shipping_cost: float,
    packaging_cost: float,
    etsy_fee_rate: float = DEFAULT_ETSY_FEE_RATE,
    min_margin: float = DEFAULT_MIN_MARGIN,
) -> float:
    """
    Calculate the minimum profitable price (floor price).

    Formula: (print_cost + shipping_cost + packaging_cost) / (1 - etsy_fee_rate) / (1 - min_margin)

    Args:
        print_cost: Cost to print one sticker.
        shipping_cost: Cost to ship one sticker.
        packaging_cost: Cost for envelope/mailer.
        etsy_fee_rate: Etsy transaction + payment fee rate (default 10%).
        min_margin: Minimum profit margin target (default 20%).

    Returns:
        Floor price in USD (not yet rounded to price point).
    """
    total_cost = print_cost + shipping_cost + packaging_cost
    if etsy_fee_rate >= 1.0 or min_margin >= 1.0:
        logger.warning(
            "Invalid fee_rate=%.2f or margin=%.2f, using defaults",
            etsy_fee_rate, min_margin,
        )
        etsy_fee_rate = DEFAULT_ETSY_FEE_RATE
        min_margin = DEFAULT_MIN_MARGIN

    floor = total_cost / (1 - etsy_fee_rate) / (1 - min_margin)
    return round(floor, 2)


class PricingTierManager:
    """
    Manages pricing tier lookups from the Supabase pricing_tiers table.

    Provides methods to:
      - Get the correct tier for a given trend age
      - Look up the price for a tier and product type
      - Calculate floor prices with cost data from shipping_rates
    """

    def __init__(self, db: Optional[SupabaseClient] = None) -> None:
        self._db = db or SupabaseClient()
        self._tiers: Optional[List[Dict[str, Any]]] = None

    def _load_tiers(self) -> List[Dict[str, Any]]:
        """Load pricing tiers from the database, with fallback to defaults."""
        if self._tiers is not None:
            return self._tiers

        try:
            tiers = self._db.get_pricing_tiers()
            if tiers:
                self._tiers = tiers
                logger.info("Loaded %d pricing tiers from database", len(tiers))
                return self._tiers
        except DatabaseError as exc:
            logger.error("Failed to load pricing tiers: %s", exc)

        logger.warning("Using default pricing tiers (DB unavailable)")
        self._tiers = DEFAULT_TIERS
        return self._tiers

    def get_tier_for_age(self, age_days: int) -> str:
        """
        Determine the pricing tier name based on trend age.

        Args:
            age_days: Number of days since the trend was created.

        Returns:
            Tier name string ('just_dropped', 'trending', 'cooling', 'evergreen').
        """
        tiers = self._load_tiers()

        for tier in tiers:
            min_age = tier.get("min_trend_age_days", 0) or 0
            max_age = tier.get("max_trend_age_days")

            if max_age is None:
                # Open-ended tier (evergreen)
                if age_days >= min_age:
                    return tier["tier"]
            else:
                if min_age <= age_days < max_age:
                    return tier["tier"]

        # Fallback: if no tier matches, use evergreen
        logger.warning("No tier matched for age=%d days, defaulting to 'evergreen'", age_days)
        return "evergreen"

    def get_price(self, tier: str, product_type: str = "single_small") -> float:
        """
        Look up the listed price for a tier and product type.

        Args:
            tier: Pricing tier name.
            product_type: 'single_small' (3") or 'single_large' (4").

        Returns:
            Price in USD.
        """
        tiers = self._load_tiers()
        price_key = (
            "price_single_small" if product_type == "single_small"
            else "price_single_large"
        )

        for t in tiers:
            if t["tier"] == tier:
                price = t.get(price_key)
                if price is not None:
                    return float(price)
                break

        # Fallback defaults
        defaults = {
            "just_dropped": {"single_small": 5.49, "single_large": 6.49},
            "trending": {"single_small": 4.49, "single_large": 5.49},
            "cooling": {"single_small": 3.49, "single_large": 4.49},
            "evergreen": {"single_small": 3.49, "single_large": 4.49},
        }
        fallback = defaults.get(tier, defaults["evergreen"])
        clean_type = "single_small" if product_type == "single_small" else "single_large"
        logger.warning("Using fallback price for tier=%s type=%s", tier, clean_type)
        return fallback[clean_type]

    def get_floor_price(
        self,
        product_type: str = "single_small",
        fulfillment_provider: str = "sticker_mule",
        print_cost: float = 1.50,
    ) -> float:
        """
        Calculate the floor price for a product type and fulfillment provider.

        Reads shipping and packaging costs from the shipping_rates table.

        Args:
            product_type: 'single_small' or 'single_large'.
            fulfillment_provider: 'sticker_mule' or 'self_usps'.
            print_cost: Base print cost (default $1.50 for 3", $2.00 for 4").

        Returns:
            Floor price rounded to a .49/.99 price point.
        """
        if product_type == "single_large" and print_cost == 1.50:
            print_cost = 2.00

        shipping_cost = 0.0
        packaging_cost = 0.0

        try:
            rate = self._db.get_shipping_rate(product_type, fulfillment_provider)
            if rate:
                shipping_cost = float(rate.get("shipping_cost", 0))
                packaging_cost = float(rate.get("packaging_cost", 0))
        except DatabaseError as exc:
            logger.error("Failed to load shipping rate: %s", exc)
            # Use fallback costs for self_usps
            if fulfillment_provider == "self_usps":
                shipping_cost = 0.78
                packaging_cost = 0.15 if product_type == "single_small" else 0.20

        raw_floor = calculate_floor_price(print_cost, shipping_cost, packaging_cost)
        return round_to_price_point(raw_floor)

    def reload(self) -> None:
        """Force reload tiers from the database on next access."""
        self._tiers = None
