"""
Daily pricing engine for Sticker Trendz.

Runs at 6AM UTC via GitHub Actions. Adjusts sticker prices based on trend
age, enforces sales override (10+ units keeps price), calculates floor
prices, updates Etsy listing prices, and logs all changes to price_history.
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

from src.config import load_config, setup_logging
from src.db import SupabaseClient, DatabaseError
from src.monitoring.pipeline_logger import PipelineRunLogger
from src.monitoring.error_logger import ErrorLogger
from src.monitoring.alerter import EmailAlerter
from src.publisher.etsy import EtsyPublisher
from src.publisher.etsy_auth import EtsyAuthManager
from src.publisher.etsy_rate_limiter import EtsyRateLimiter, P2_PRICE_UPDATES
from src.pricing.tiers import PricingTierManager, round_to_price_point
from src.pricing.archiver import StickerArchiver

logger = logging.getLogger(__name__)

WORKFLOW_NAME = "pricing_engine"

# Minimum sales count at the current tier to trigger sales override
SALES_OVERRIDE_THRESHOLD = 10


class PricingEngine:
    """
    Daily pricing engine that adjusts sticker prices based on trend freshness.

    Pricing tiers:
      - just_dropped (0-3 days): highest price
      - trending (4-14 days): standard price
      - cooling (15-30 days): discounted
      - evergreen (30+ days with sales): same as cooling
      - archived (30+ days, 0 sales/views for 14 days): delisted

    Sales override: 10+ sales at the current tier keeps the price unchanged.
    Floor price: never price below cost + fees + 20% margin.
    All prices end in .49 or .99.
    """

    def __init__(
        self,
        db: Optional[SupabaseClient] = None,
        tier_manager: Optional[PricingTierManager] = None,
        archiver: Optional[StickerArchiver] = None,
        etsy_publisher: Optional[EtsyPublisher] = None,
        rate_limiter: Optional[EtsyRateLimiter] = None,
        pipeline_logger: Optional[PipelineRunLogger] = None,
        error_logger: Optional[ErrorLogger] = None,
        alerter: Optional[EmailAlerter] = None,
    ) -> None:
        self._db = db or SupabaseClient()
        self._tiers = tier_manager or PricingTierManager(self._db)
        self._archiver = archiver
        self._publisher = etsy_publisher
        self._rate_limiter = rate_limiter
        self._pipeline_logger = pipeline_logger or PipelineRunLogger(self._db)
        self._error_logger = error_logger or ErrorLogger(self._db)
        self._alerter = alerter

    def run(self) -> Dict[str, int]:
        """
        Execute the daily pricing engine.

        Steps:
          1. Acquire concurrency lock
          2. Check Etsy API rate limit budget
          3. Run archiver (free listing slots)
          4. Fetch all published stickers
          5. For each sticker: determine tier, check overrides, update price
          6. Log all changes to price_history
          7. Send summary email

        Returns:
            Dict with counts: repriced, archived, skipped, errors.
        """
        run_id = self._pipeline_logger.start_run(WORKFLOW_NAME)
        counts = {
            "prices_updated": 0,
            "stickers_archived": 0,
            "errors_count": 0,
        }
        etsy_api_calls = 0

        try:
            # Step 1: Acquire concurrency lock
            if self._rate_limiter:
                if not self._rate_limiter.acquire_lock(WORKFLOW_NAME):
                    logger.info("Another pricing engine is running, exiting")
                    self._pipeline_logger.complete_run(
                        run_id, counts=counts,
                        metadata={"skipped": "lock_held"},
                    )
                    return counts

            # Step 2: Check Etsy API rate limit budget
            if self._rate_limiter:
                if not self._rate_limiter.can_proceed(P2_PRICE_UPDATES):
                    logger.warning("Etsy API rate limit too high, skipping price updates")
                    self._pipeline_logger.complete_run(
                        run_id, counts=counts,
                        metadata={"skipped": "rate_limit"},
                    )
                    return counts

            # Step 3: Run archiver first to free listing slots
            if self._archiver:
                archived = self._archiver.run()
                counts["stickers_archived"] = archived

            # Step 4: Fetch all published stickers with their trends
            try:
                stickers = self._db.get_published_stickers()
            except DatabaseError as exc:
                logger.error("Failed to fetch published stickers: %s", exc)
                self._pipeline_logger.fail_run(
                    run_id,
                    error_message=f"Failed to fetch stickers: {exc}",
                    counts=counts,
                )
                return counts

            logger.info("Processing %d published stickers for repricing", len(stickers))

            # Step 5: Process each sticker
            for sticker in stickers:
                # Skip already archived stickers
                if sticker.get("moderation_status") == "archived":
                    continue
                if sticker.get("current_pricing_tier") == "archived":
                    continue

                try:
                    updated = self._process_sticker(sticker)
                    if updated:
                        counts["prices_updated"] += 1
                        etsy_api_calls += 1
                except Exception as exc:
                    counts["errors_count"] += 1
                    logger.error(
                        "Error processing sticker %s: %s",
                        sticker.get("id", ""), exc,
                    )
                    self._error_logger.log_error(
                        workflow=WORKFLOW_NAME,
                        step="reprice",
                        error_type="processing_error",
                        error_message=str(exc),
                        service="pricing",
                        pipeline_run_id=run_id,
                        context={"sticker_id": sticker.get("id", "")},
                    )

            # Step 6: Complete run
            self._pipeline_logger.complete_run(
                run_id,
                counts=counts,
                etsy_api_calls_used=etsy_api_calls,
            )

            # Step 7: Send summary email
            if self._alerter:
                try:
                    active_count = self._db.count_active_listings()
                    self._alerter.send_daily_summary(
                        pipeline_health={
                            "pricing_engine": "completed",
                            "stickers_processed": len(stickers),
                        },
                        revenue={},
                        pricing={
                            "repriced": counts["prices_updated"],
                            "archived": counts["stickers_archived"],
                            "below_floor": 0,
                            "active_listings": active_count,
                            "max_listings": 300,
                        },
                        costs={"api_calls": etsy_api_calls},
                        alerts=[],
                    )
                except Exception as exc:
                    logger.warning("Failed to send pricing summary email: %s", exc)

            logger.info(
                "Pricing engine complete: %d repriced, %d archived, %d errors",
                counts["prices_updated"],
                counts["stickers_archived"],
                counts["errors_count"],
            )

        except Exception as exc:
            logger.error("Pricing engine failed: %s", exc)
            self._pipeline_logger.fail_run(
                run_id,
                error_message=str(exc),
                counts=counts,
                etsy_api_calls_used=etsy_api_calls,
            )
            if self._alerter:
                self._alerter.send_alert(
                    "Pricing engine failed",
                    f"Unhandled error: {str(exc)[:500]}",
                )
            raise
        finally:
            if self._rate_limiter:
                self._rate_limiter.release_lock(WORKFLOW_NAME)

        return counts

    def _process_sticker(self, sticker: Dict[str, Any]) -> bool:
        """
        Process a single sticker for repricing.

        Args:
            sticker: Sticker dict from Supabase (with joined trend data).

        Returns:
            True if the price was actually changed on Etsy.
        """
        sticker_id = sticker.get("id", "")
        listing_id = sticker.get("etsy_listing_id", "")
        current_price = float(sticker.get("price", 0))
        current_tier = sticker.get("current_pricing_tier", "just_dropped")
        product_type = "single_large" if sticker.get("size") == "4in" else "single_small"
        fulfillment_provider = sticker.get("fulfillment_provider", "sticker_mule")

        # Calculate trend age
        trend = sticker.get("trends") or {}
        trend_created_str = trend.get("created_at", "") or sticker.get("created_at", "")
        trend_age_days = self._calculate_trend_age(trend_created_str)

        # Determine new tier
        new_tier = self._tiers.get_tier_for_age(trend_age_days)

        # Check if sticker should be archived (30+ days, no recent sales)
        if trend_age_days >= 30:
            has_recent_sales = self._has_recent_sales(sticker_id)
            if not has_recent_sales:
                # Let the archiver handle this; mark as evergreen if it has any sales
                sales_count = sticker.get("sales_count", 0) or 0
                if sales_count == 0:
                    # This will be caught by the archiver
                    return False
                # Has historical sales but none recent -- keep as evergreen
                new_tier = "evergreen"

        # Check sales override: 10+ sales at current tier keeps price
        if self._check_sales_override(sticker_id, current_tier):
            logger.debug(
                "Sales override for sticker %s: %d+ sales at tier '%s', keeping price",
                sticker_id, SALES_OVERRIDE_THRESHOLD, current_tier,
            )
            # Still update the tier if it changed, but keep the price
            if new_tier != current_tier:
                try:
                    self._db.update_sticker(sticker_id, {
                        "current_pricing_tier": new_tier,
                    })
                except DatabaseError:
                    pass
            return False

        # Look up the new tier price
        new_price = self._tiers.get_price(new_tier, product_type)

        # Calculate and enforce floor price
        floor_price = self._tiers.get_floor_price(
            product_type=product_type,
            fulfillment_provider=fulfillment_provider,
        )

        if new_price < floor_price:
            logger.info(
                "Price $%.2f below floor $%.2f for sticker %s, using floor",
                new_price, floor_price, sticker_id,
            )
            new_price = floor_price

        # Round to .49 or .99
        new_price = round_to_price_point(new_price)

        # Compare with current price
        if abs(new_price - current_price) < 0.01 and new_tier == current_tier:
            # No change needed
            return False

        # Update Etsy listing price
        if listing_id and self._publisher:
            success = self._publisher.update_listing_price(listing_id, new_price)
            if not success:
                logger.warning(
                    "Failed to update Etsy price for sticker %s (listing %s)",
                    sticker_id, listing_id,
                )
                return False

        # Update sticker record in Supabase
        try:
            self._db.update_sticker(sticker_id, {
                "price": new_price,
                "current_pricing_tier": new_tier,
                "floor_price": floor_price,
            })
        except DatabaseError as exc:
            logger.error("Failed to update sticker %s price: %s", sticker_id, exc)

        # Log to price_history
        reason = "trend_age"
        if new_tier != current_tier:
            reason = f"tier_change:{current_tier}->{new_tier}"

        try:
            self._db.insert_price_history({
                "sticker_id": sticker_id,
                "old_price": current_price,
                "new_price": new_price,
                "pricing_tier": new_tier,
                "reason": reason,
            })
        except DatabaseError as exc:
            logger.warning("Failed to log price_history for sticker %s: %s", sticker_id, exc)

        logger.info(
            "Repriced sticker %s: $%.2f -> $%.2f (tier: %s -> %s)",
            sticker_id, current_price, new_price, current_tier, new_tier,
        )
        return True

    @staticmethod
    def _calculate_trend_age(created_at_str: str) -> int:
        """Calculate trend age in days from a created_at timestamp string."""
        if not created_at_str:
            return 0
        try:
            created_at = datetime.fromisoformat(
                created_at_str.replace("Z", "+00:00")
            )
            delta = datetime.now(timezone.utc) - created_at
            return max(0, delta.days)
        except (ValueError, AttributeError):
            return 0

    def _has_recent_sales(self, sticker_id: str, days: int = 14) -> bool:
        """Check if a sticker has any sales in the last N days."""
        try:
            orders = self._db.select(
                "orders",
                columns="id",
                filters={"sticker_id": sticker_id},
                limit=1,
            )
            if not orders:
                return False

            # Check if last_sale_at is within the window
            sticker_rows = self._db.select(
                "stickers",
                columns="last_sale_at",
                filters={"id": sticker_id},
                limit=1,
            )
            if not sticker_rows:
                return False

            last_sale_str = sticker_rows[0].get("last_sale_at", "")
            if not last_sale_str:
                return False

            last_sale = datetime.fromisoformat(last_sale_str.replace("Z", "+00:00"))
            cutoff = datetime.now(timezone.utc) - timedelta(days=days)
            return last_sale >= cutoff

        except DatabaseError as exc:
            logger.error("Failed to check recent sales for sticker %s: %s", sticker_id, exc)
            return False

    def _check_sales_override(self, sticker_id: str, current_tier: str) -> bool:
        """
        Check if a sticker has 10+ sales at the current pricing tier.

        The sales override means the price stays unchanged because
        demand is proven at this price point.

        Args:
            sticker_id: Sticker UUID.
            current_tier: Current pricing tier name.

        Returns:
            True if the override should apply (keep current price).
        """
        try:
            orders = self._db.select(
                "orders",
                columns="id",
                filters={
                    "sticker_id": sticker_id,
                    "pricing_tier_at_sale": current_tier,
                },
            )
            count = len(orders)
            return count >= SALES_OVERRIDE_THRESHOLD
        except DatabaseError as exc:
            logger.error("Failed to check sales override for sticker %s: %s", sticker_id, exc)
            return False


def main() -> None:
    """Entry point for `python -m src.pricing.engine`."""
    setup_logging()
    logger.info("Starting daily pricing engine")

    try:
        cfg = load_config(require_all=False)
    except Exception as exc:
        logger.critical("Failed to load config: %s", exc)
        sys.exit(1)

    db = SupabaseClient()
    rate_limiter = EtsyRateLimiter()
    auth = EtsyAuthManager(db=db, alerter=EmailAlerter())
    error_logger = ErrorLogger(db)
    publisher = EtsyPublisher(
        db=db,
        auth=auth,
        rate_limiter=rate_limiter,
        error_logger=error_logger,
    )
    archiver = StickerArchiver(
        db=db,
        etsy_publisher=publisher,
        error_logger=error_logger,
    )

    engine = PricingEngine(
        db=db,
        tier_manager=PricingTierManager(db),
        archiver=archiver,
        etsy_publisher=publisher,
        rate_limiter=rate_limiter,
        error_logger=error_logger,
        alerter=EmailAlerter(),
    )

    try:
        counts = engine.run()
        logger.info("Pricing engine finished: %s", counts)
        sys.exit(0)
    except Exception as exc:
        logger.critical("Pricing engine failed: %s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
