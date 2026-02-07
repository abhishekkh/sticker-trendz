"""
Daily metrics aggregation for Sticker Trendz.

Computes daily business metrics: orders, revenue, COGS, Etsy fees,
profit, new listings, average order value, AI spend, and API usage.
Supports the daily summary email and cost tracking view.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from src.db import SupabaseClient, DatabaseError

logger = logging.getLogger(__name__)


class MetricsAggregator:
    """
    Aggregates daily and monthly business metrics from Supabase.

    Reads from orders, stickers, and pipeline_runs tables to compute
    revenue, cost, and operational metrics.
    """

    def __init__(self, db: Optional[SupabaseClient] = None) -> None:
        self._db = db or SupabaseClient()

    def get_daily_metrics(self, date: Optional[datetime] = None) -> Dict[str, Any]:
        """
        Compute metrics for a specific date.

        Args:
            date: Date to aggregate. Defaults to today (UTC).

        Returns:
            Dict with orders, gross_revenue, cogs, etsy_fees,
            estimated_profit, new_listings, avg_order_value.
        """
        target = date or datetime.now(timezone.utc)
        date_str = target.strftime("%Y-%m-%d")

        metrics: Dict[str, Any] = {
            "date": date_str,
            "orders": 0,
            "gross_revenue": 0.0,
            "cogs": 0.0,
            "etsy_fees": 0.0,
            "estimated_profit": 0.0,
            "new_listings": 0,
            "avg_order_value": 0.0,
        }

        # Fetch orders for the date
        try:
            all_orders = self._db.select("orders", columns="*")
            day_orders = [
                o for o in all_orders
                if o.get("created_at", "").startswith(date_str)
                and o.get("status") != "refunded"
            ]

            metrics["orders"] = len(day_orders)

            if day_orders:
                gross = sum(float(o.get("total_amount", 0) or 0) for o in day_orders)
                metrics["gross_revenue"] = round(gross, 2)

                # Estimate COGS from sticker base_cost
                cogs = 0.0
                for order in day_orders:
                    sticker_id = order.get("sticker_id")
                    qty = int(order.get("quantity", 1) or 1)
                    if sticker_id:
                        try:
                            sticker_rows = self._db.select(
                                "stickers",
                                columns="base_cost",
                                filters={"id": sticker_id},
                                limit=1,
                            )
                            if sticker_rows:
                                base_cost = float(sticker_rows[0].get("base_cost", 1.50) or 1.50)
                                cogs += base_cost * qty
                        except DatabaseError:
                            cogs += 1.50 * qty  # fallback estimate
                    else:
                        cogs += 1.50 * qty

                metrics["cogs"] = round(cogs, 2)
                metrics["etsy_fees"] = round(gross * 0.10, 2)
                metrics["estimated_profit"] = round(
                    gross - cogs - metrics["etsy_fees"], 2
                )
                metrics["avg_order_value"] = round(gross / len(day_orders), 2)

        except DatabaseError as exc:
            logger.error("Failed to compute daily order metrics: %s", exc)

        # Count new listings published today
        try:
            all_stickers = self._db.select("stickers", columns="published_at")
            new_listings = sum(
                1 for s in all_stickers
                if s.get("published_at", "").startswith(date_str)
            )
            metrics["new_listings"] = new_listings
        except DatabaseError as exc:
            logger.error("Failed to count new listings: %s", exc)

        logger.info("Daily metrics for %s: %s", date_str, metrics)
        return metrics

    def get_mtd_metrics(self) -> Dict[str, Any]:
        """
        Compute month-to-date aggregate metrics.

        Returns:
            Dict with mtd_orders, mtd_revenue, mtd_profit, mtd_cogs, mtd_fees.
        """
        now = datetime.now(timezone.utc)
        prefix = now.strftime("%Y-%m")

        mtd: Dict[str, Any] = {
            "month": prefix,
            "mtd_orders": 0,
            "mtd_revenue": 0.0,
            "mtd_cogs": 0.0,
            "mtd_fees": 0.0,
            "mtd_profit": 0.0,
        }

        try:
            all_orders = self._db.select("orders", columns="*")
            month_orders = [
                o for o in all_orders
                if o.get("created_at", "").startswith(prefix)
                and o.get("status") != "refunded"
            ]

            mtd["mtd_orders"] = len(month_orders)
            if month_orders:
                gross = sum(float(o.get("total_amount", 0) or 0) for o in month_orders)
                mtd["mtd_revenue"] = round(gross, 2)
                mtd["mtd_fees"] = round(gross * 0.10, 2)

                # Estimate COGS
                cogs = sum(
                    float(o.get("quantity", 1) or 1) * 1.50
                    for o in month_orders
                )
                mtd["mtd_cogs"] = round(cogs, 2)
                mtd["mtd_profit"] = round(gross - cogs - mtd["mtd_fees"], 2)

        except DatabaseError as exc:
            logger.error("Failed to compute MTD metrics: %s", exc)

        logger.info("MTD metrics for %s: %s", prefix, mtd)
        return mtd

    def get_ai_spend(self, date: Optional[datetime] = None) -> float:
        """
        Get total AI cost for a specific date from pipeline_runs.

        Args:
            date: Date to query. Defaults to today.

        Returns:
            Total ai_cost_estimate_usd for the day.
        """
        target = date or datetime.now(timezone.utc)
        date_str = target.strftime("%Y-%m-%d")

        try:
            rows = self._db.select(
                "pipeline_runs",
                columns="ai_cost_estimate_usd,started_at",
            )
            total = sum(
                float(r.get("ai_cost_estimate_usd", 0) or 0)
                for r in rows
                if r.get("started_at", "").startswith(date_str)
            )
            return round(total, 4)
        except DatabaseError as exc:
            logger.error("Failed to get AI spend for %s: %s", date_str, exc)
            return 0.0

    def get_api_usage(self, date: Optional[datetime] = None) -> int:
        """
        Get total Etsy API calls used for a specific date from pipeline_runs.

        Args:
            date: Date to query. Defaults to today.

        Returns:
            Total etsy_api_calls_used for the day.
        """
        target = date or datetime.now(timezone.utc)
        date_str = target.strftime("%Y-%m-%d")

        try:
            rows = self._db.select(
                "pipeline_runs",
                columns="etsy_api_calls_used,started_at",
            )
            total = sum(
                int(r.get("etsy_api_calls_used", 0) or 0)
                for r in rows
                if r.get("started_at", "").startswith(date_str)
            )
            return total
        except DatabaseError as exc:
            logger.error("Failed to get API usage for %s: %s", date_str, exc)
            return 0

    def refresh_materialized_view(self) -> bool:
        """
        Refresh the daily_metrics materialized view in Supabase.

        Returns:
            True if the refresh succeeded.
        """
        try:
            self._db.rpc("refresh_daily_metrics", {})
            logger.info("Refreshed daily_metrics materialized view")
            return True
        except DatabaseError as exc:
            logger.error("Failed to refresh daily_metrics view: %s", exc)
            return False
