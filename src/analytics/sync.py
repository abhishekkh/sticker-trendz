"""
Daily analytics sync for Sticker Trendz.

Runs at 8AM UTC via GitHub Actions. Syncs sales data from the Etsy API,
creates order records, updates sticker metrics, triggers fulfillment for
new orders, checks flagged sticker timeouts, refreshes materialized views,
runs PII purge, and sends the daily summary email.
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import httpx

from src.config import load_config, setup_logging
from src.db import SupabaseClient, DatabaseError
from src.monitoring.pipeline_logger import PipelineRunLogger
from src.monitoring.error_logger import ErrorLogger
from src.monitoring.alerter import EmailAlerter
from src.monitoring.spend_tracker import SpendTracker
from src.publisher.etsy_auth import EtsyAuthManager, OAuthError
from src.publisher.etsy_rate_limiter import EtsyRateLimiter, P0_ORDER_READS, P3_ANALYTICS
from src.moderation.moderator import ContentModerator
from src.analytics.metrics import MetricsAggregator
from src.analytics.pii_purge import PIIPurger

logger = logging.getLogger(__name__)

WORKFLOW_NAME = "analytics_sync"
ETSY_API_BASE = "https://openapi.etsy.com/v3/application"


class AnalyticsSync:
    """
    Daily analytics synchronization engine.

    Orchestrates:
      1. Fetch new orders from Etsy
      2. Update sticker sales/view counts
      3. Trigger fulfillment for new orders
      4. Check flagged sticker timeouts (48h auto-reject)
      5. Refresh daily_metrics materialized view
      6. Run PII purge
      7. Send daily summary email
    """

    def __init__(
        self,
        db: Optional[SupabaseClient] = None,
        auth: Optional[EtsyAuthManager] = None,
        rate_limiter: Optional[EtsyRateLimiter] = None,
        pipeline_logger: Optional[PipelineRunLogger] = None,
        error_logger: Optional[ErrorLogger] = None,
        alerter: Optional[EmailAlerter] = None,
        moderator: Optional[ContentModerator] = None,
        metrics: Optional[MetricsAggregator] = None,
        pii_purger: Optional[PIIPurger] = None,
        spend_tracker: Optional[SpendTracker] = None,
        shop_id: Optional[str] = None,
        etsy_api_key: Optional[str] = None,
        http_client: Optional[httpx.Client] = None,
        fulfillment_router: Optional[Any] = None,
    ) -> None:
        self._db = db or SupabaseClient()
        self._auth = auth
        self._rate_limiter = rate_limiter
        self._pipeline_logger = pipeline_logger or PipelineRunLogger(self._db)
        self._error_logger = error_logger or ErrorLogger(self._db)
        self._alerter = alerter
        self._moderator = moderator
        self._metrics = metrics or MetricsAggregator(self._db)
        self._pii_purger = pii_purger
        self._spend_tracker = spend_tracker
        self._fulfillment_router = fulfillment_router

        cfg = load_config(require_all=False)
        self._shop_id = shop_id or cfg.etsy.shop_id
        self._api_key = etsy_api_key or cfg.etsy.api_key
        self._http = http_client or httpx.Client(timeout=30)

    def _get_headers(self) -> Dict[str, str]:
        """Get authorization headers for Etsy API calls."""
        if self._auth:
            token = self._auth.get_access_token(self._shop_id)
            return {
                "Authorization": f"Bearer {token}",
                "x-api-key": self._api_key,
            }
        return {"x-api-key": self._api_key}

    def _track_api_call(self, count: int = 1) -> None:
        """Track Etsy API call usage."""
        if self._rate_limiter:
            self._rate_limiter.increment_api_calls(count)

    def run(self) -> Dict[str, int]:
        """
        Execute the full daily analytics sync.

        Returns:
            Dict with counts: orders_synced, orders_fulfilled,
            stickers_auto_rejected, errors_count.
        """
        run_id = self._pipeline_logger.start_run(WORKFLOW_NAME)
        counts: Dict[str, int] = {
            "orders_synced": 0,
            "orders_fulfilled": 0,
            "stickers_archived": 0,
            "errors_count": 0,
        }
        etsy_api_calls = 0

        try:
            # Acquire concurrency lock
            if self._rate_limiter:
                if not self._rate_limiter.acquire_lock(WORKFLOW_NAME):
                    logger.info("Another analytics sync is running, exiting")
                    self._pipeline_logger.complete_run(
                        run_id, counts=counts,
                        metadata={"skipped": "lock_held"},
                    )
                    return counts

            # Step 1: Fetch new orders from Etsy
            if self._rate_limiter and self._rate_limiter.can_proceed(P0_ORDER_READS):
                try:
                    new_orders = self._fetch_etsy_orders()
                    etsy_api_calls += 1

                    for order_data in new_orders:
                        try:
                            self._process_order(order_data)
                            counts["orders_synced"] += 1
                        except Exception as exc:
                            counts["errors_count"] += 1
                            logger.error("Error processing order: %s", exc)
                            self._error_logger.log_error(
                                workflow=WORKFLOW_NAME,
                                step="order_sync",
                                error_type="processing_error",
                                error_message=str(exc),
                                service="etsy",
                                pipeline_run_id=run_id,
                            )

                except OAuthError as exc:
                    logger.error("Etsy OAuth error during order sync: %s", exc)
                    counts["errors_count"] += 1
                except Exception as exc:
                    logger.error("Failed to fetch Etsy orders: %s", exc)
                    counts["errors_count"] += 1
                    self._error_logger.log_error(
                        workflow=WORKFLOW_NAME,
                        step="order_fetch",
                        error_type="api_error",
                        error_message=str(exc),
                        service="etsy",
                        pipeline_run_id=run_id,
                    )

            # Step 2: Update sticker view counts from Etsy
            if self._rate_limiter and self._rate_limiter.can_proceed(P3_ANALYTICS):
                try:
                    self._update_listing_stats()
                    etsy_api_calls += 1
                except Exception as exc:
                    logger.error("Failed to update listing stats: %s", exc)
                    counts["errors_count"] += 1

            # Step 3: Trigger fulfillment for new orders
            if self._fulfillment_router:
                try:
                    pending_orders = self._db.get_orders_by_status("paid")
                    for order in pending_orders:
                        try:
                            self._fulfillment_router.fulfill_order(order)
                            counts["orders_fulfilled"] += 1
                        except Exception as exc:
                            logger.error("Fulfillment failed for order %s: %s", order.get("id"), exc)
                            counts["errors_count"] += 1
                except DatabaseError as exc:
                    logger.error("Failed to fetch pending orders: %s", exc)

            # Step 4: Check flagged sticker timeouts (48h auto-reject)
            if self._moderator:
                try:
                    auto_rejected = self._moderator.check_flagged_timeout()
                    counts["stickers_archived"] = auto_rejected
                except Exception as exc:
                    logger.error("Flagged sticker check failed: %s", exc)

            # Step 5: Refresh materialized view
            self._metrics.refresh_materialized_view()

            # Step 6: Run PII purge
            if self._pii_purger:
                try:
                    purge_results = self._pii_purger.run_all()
                    logger.info("PII purge results: %s", purge_results)
                except Exception as exc:
                    logger.error("PII purge failed: %s", exc)
                    counts["errors_count"] += 1

            # Step 7: Send daily summary email
            if self._alerter:
                self._send_daily_summary(counts, etsy_api_calls)

            # Complete pipeline run
            self._pipeline_logger.complete_run(
                run_id,
                counts=counts,
                etsy_api_calls_used=etsy_api_calls,
            )

            logger.info("Analytics sync complete: %s", counts)

        except Exception as exc:
            logger.error("Analytics sync failed: %s", exc)
            self._pipeline_logger.fail_run(
                run_id,
                error_message=str(exc),
                counts=counts,
                etsy_api_calls_used=etsy_api_calls,
            )
            if self._alerter:
                self._alerter.send_alert(
                    "Analytics sync failed",
                    f"Unhandled error: {str(exc)[:500]}",
                )
            raise
        finally:
            if self._rate_limiter:
                self._rate_limiter.release_lock(WORKFLOW_NAME)

        return counts

    def _fetch_etsy_orders(self) -> List[Dict[str, Any]]:
        """
        Fetch recent orders (receipts) from the Etsy API.

        Returns:
            List of order data dicts from Etsy.
        """
        try:
            headers = self._get_headers()
            url = f"{ETSY_API_BASE}/shops/{self._shop_id}/receipts"
            params = {
                "was_paid": "true",
                "limit": 100,
            }
            response = self._http.get(url, params=params, headers=headers)
            self._track_api_call()

            if response.status_code == 429:
                logger.warning("Etsy rate limit hit during order fetch")
                return []

            response.raise_for_status()
            data = response.json()
            results = data.get("results", [])
            logger.info("Fetched %d receipts from Etsy", len(results))
            return results

        except httpx.HTTPStatusError as exc:
            logger.error("Etsy API error fetching orders: %s", exc)
            raise
        except Exception as exc:
            logger.error("Failed to fetch Etsy orders: %s", exc)
            raise

    def _process_order(self, etsy_receipt: Dict[str, Any]) -> None:
        """
        Process a single Etsy receipt into an order record.

        Args:
            etsy_receipt: Receipt data from the Etsy API.
        """
        receipt_id = str(etsy_receipt.get("receipt_id", ""))

        # Check if we already have this order
        try:
            existing = self._db.select(
                "orders",
                columns="id",
                filters={"etsy_receipt_id": receipt_id},
                limit=1,
            )
            if existing:
                return  # Already synced
        except DatabaseError:
            pass

        # Extract order details from Etsy receipt
        transactions = etsy_receipt.get("transactions", [])
        for transaction in transactions:
            listing_id = str(transaction.get("listing_id", ""))
            quantity = int(transaction.get("quantity", 1))
            price = float(transaction.get("price", {}).get("amount", 0)) / 100  # Etsy returns cents
            title = transaction.get("title", "")

            # Find the sticker by listing_id
            sticker_id = None
            sticker_tier = None
            try:
                sticker_rows = self._db.select(
                    "stickers",
                    columns="id,current_pricing_tier",
                    filters={"etsy_listing_id": listing_id},
                    limit=1,
                )
                if sticker_rows:
                    sticker_id = sticker_rows[0]["id"]
                    sticker_tier = sticker_rows[0].get("current_pricing_tier")
            except DatabaseError:
                pass

            # Create order record
            order_data: Dict[str, Any] = {
                "etsy_order_id": str(transaction.get("transaction_id", "")),
                "etsy_receipt_id": receipt_id,
                "quantity": quantity,
                "unit_price": price,
                "total_amount": price * quantity,
                "status": "paid",
                "pricing_tier_at_sale": sticker_tier,
                "customer_data": self._extract_customer_data(etsy_receipt),
            }
            if sticker_id:
                order_data["sticker_id"] = sticker_id

            try:
                self._db.insert_order(order_data)
                logger.info("Created order for listing %s (qty=%d)", listing_id, quantity)

                # Update sticker sales count
                if sticker_id:
                    self._increment_sales_count(sticker_id, quantity)

            except DatabaseError as exc:
                logger.error("Failed to create order for receipt %s: %s", receipt_id, exc)
                raise

    def _increment_sales_count(self, sticker_id: str, quantity: int) -> None:
        """Increment the sales_count on a sticker record."""
        try:
            rows = self._db.select(
                "stickers",
                columns="sales_count",
                filters={"id": sticker_id},
                limit=1,
            )
            if rows:
                current = int(rows[0].get("sales_count", 0) or 0)
                self._db.update_sticker(sticker_id, {
                    "sales_count": current + quantity,
                    "last_sale_at": datetime.now(timezone.utc).isoformat(),
                })
        except DatabaseError as exc:
            logger.error("Failed to update sales_count for sticker %s: %s", sticker_id, exc)

    def _update_listing_stats(self) -> None:
        """Update view counts for published stickers from Etsy."""
        try:
            stickers = self._db.get_published_stickers()
            for sticker in stickers:
                listing_id = sticker.get("etsy_listing_id")
                if not listing_id:
                    continue

                try:
                    headers = self._get_headers()
                    url = f"{ETSY_API_BASE}/listings/{listing_id}"
                    response = self._http.get(url, headers=headers)
                    self._track_api_call()

                    if response.status_code == 200:
                        data = response.json()
                        views = data.get("views", 0)
                        self._db.update_sticker(sticker["id"], {
                            "view_count": views,
                        })
                except Exception as exc:
                    logger.debug("Failed to update stats for listing %s: %s", listing_id, exc)

        except DatabaseError as exc:
            logger.error("Failed to fetch stickers for stats update: %s", exc)

    @staticmethod
    def _extract_customer_data(receipt: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        Extract customer data from an Etsy receipt for fulfillment.

        This data will be purged after 90 days per data retention policy.
        """
        name = receipt.get("name", "")
        address = receipt.get("formatted_address", "")

        if not name and not address:
            return None

        return {
            "name": name,
            "address": address,
            "city": receipt.get("city", ""),
            "state": receipt.get("state", ""),
            "zip": receipt.get("zip", ""),
            "country": receipt.get("country_iso", "US"),
        }

    def _send_daily_summary(self, counts: Dict[str, int], api_calls: int) -> None:
        """Send the daily summary email."""
        if not self._alerter:
            return

        try:
            daily = self._metrics.get_daily_metrics()
            mtd = self._metrics.get_mtd_metrics()
            ai_spend = self._metrics.get_ai_spend()
            active_listings = self._db.count_active_listings()

            # Get monthly AI spend
            ai_spend_mtd = 0.0
            if self._spend_tracker:
                ai_spend_mtd = self._spend_tracker.get_monthly_spend()

            self._alerter.send_daily_summary(
                pipeline_health={
                    "analytics_sync": "completed",
                    "orders_synced": counts.get("orders_synced", 0),
                    "orders_fulfilled": counts.get("orders_fulfilled", 0),
                    "errors": counts.get("errors_count", 0),
                },
                revenue={
                    "orders": daily.get("orders", 0),
                    "gross_revenue": daily.get("gross_revenue", 0),
                    "cogs": daily.get("cogs", 0),
                    "etsy_fees": daily.get("etsy_fees", 0),
                    "estimated_profit": daily.get("estimated_profit", 0),
                    "avg_order_value": daily.get("avg_order_value", 0),
                },
                pricing={
                    "active_listings": active_listings,
                    "max_listings": 300,
                    "new_listings": daily.get("new_listings", 0),
                },
                costs={
                    "ai_spend": ai_spend,
                    "ai_spend_mtd": ai_spend_mtd,
                    "api_calls": api_calls,
                    "listing_fees": daily.get("new_listings", 0) * 0.20,
                },
                alerts=[],
            )
        except Exception as exc:
            logger.warning("Failed to send daily summary email: %s", exc)


def main() -> None:
    """Entry point for `python -m src.analytics.sync`."""
    setup_logging()
    logger.info("Starting daily analytics sync")

    try:
        cfg = load_config()
    except Exception as exc:
        logger.critical("Failed to load config: %s", exc)
        sys.exit(1)

    db = SupabaseClient()
    rate_limiter = EtsyRateLimiter()
    alerter = EmailAlerter()
    auth = EtsyAuthManager(db=db, alerter=alerter)
    error_logger = ErrorLogger(db)
    moderator = ContentModerator(db=db, alerter=alerter, error_logger=error_logger)
    pii_purger = PIIPurger(db=db)
    spend_tracker = SpendTracker(db=db)

    # Import fulfillment router
    try:
        from src.fulfillment.router import FulfillmentRouter
        fulfillment_router = FulfillmentRouter(db=db, error_logger=error_logger, alerter=alerter)
    except ImportError:
        fulfillment_router = None
        logger.warning("Fulfillment router not available")

    sync = AnalyticsSync(
        db=db,
        auth=auth,
        rate_limiter=rate_limiter,
        error_logger=error_logger,
        alerter=alerter,
        moderator=moderator,
        metrics=MetricsAggregator(db),
        pii_purger=pii_purger,
        spend_tracker=spend_tracker,
        fulfillment_router=fulfillment_router,
    )

    try:
        counts = sync.run()
        logger.info("Analytics sync finished: %s", counts)
        sys.exit(0)
    except Exception as exc:
        logger.critical("Analytics sync failed: %s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
