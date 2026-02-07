"""
PII purge job for Sticker Trendz.

Implements data retention compliance:
  - Nullifies customer_data on orders delivered 90+ days ago
  - Deletes error_log entries older than 90 days
  - Deletes pipeline_runs entries older than 180 days
  - Archives price_history entries older than 1 year to R2 as CSV
"""

from __future__ import annotations

import csv
import io
import logging
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

from src.db import SupabaseClient, DatabaseError
from src.publisher.storage import R2StorageClient, StorageError

logger = logging.getLogger(__name__)

# Retention thresholds
PII_RETENTION_DAYS = 90
ERROR_LOG_RETENTION_DAYS = 90
PIPELINE_RUNS_RETENTION_DAYS = 180
PRICE_HISTORY_RETENTION_DAYS = 365


class PIIPurger:
    """
    Manages data retention by purging PII and old records.

    Runs as part of the daily analytics sync. Ensures compliance
    with data retention policies while preserving anonymized records
    for business analytics.
    """

    def __init__(
        self,
        db: Optional[SupabaseClient] = None,
        storage: Optional[R2StorageClient] = None,
    ) -> None:
        self._db = db or SupabaseClient()
        self._storage = storage

    def purge_pii(self) -> int:
        """
        Set customer_data=NULL on orders delivered 90+ days ago.

        Preserves the order record for analytics but removes personally
        identifiable information (name, address, email).

        Returns:
            Number of orders purged.
        """
        cutoff = datetime.now(timezone.utc) - timedelta(days=PII_RETENTION_DAYS)
        cutoff_str = cutoff.isoformat()
        count = 0

        try:
            # Fetch delivered orders with customer_data still present
            delivered_orders = self._db.select(
                "orders",
                columns="id,delivered_at,customer_data",
                filters={"status": "delivered"},
            )

            for order in delivered_orders:
                # Skip if customer_data is already null
                if not order.get("customer_data"):
                    continue

                delivered_str = order.get("delivered_at", "")
                if not delivered_str:
                    continue

                try:
                    delivered_at = datetime.fromisoformat(
                        delivered_str.replace("Z", "+00:00")
                    )
                except (ValueError, AttributeError):
                    continue

                if delivered_at < cutoff:
                    try:
                        self._db.update_order(order["id"], {"customer_data": None})
                        count += 1
                    except DatabaseError as exc:
                        logger.error(
                            "Failed to purge PII for order %s: %s",
                            order["id"], exc,
                        )

        except DatabaseError as exc:
            logger.error("Failed to fetch orders for PII purge: %s", exc)

        if count > 0:
            logger.info("Purged PII from %d orders (delivered >%d days ago)", count, PII_RETENTION_DAYS)
        else:
            logger.info("No orders eligible for PII purge")

        return count

    def purge_error_logs(self) -> int:
        """
        Delete error_log entries older than 90 days.

        Returns:
            Number of entries deleted.
        """
        cutoff = datetime.now(timezone.utc) - timedelta(days=ERROR_LOG_RETENTION_DAYS)
        count = 0

        try:
            all_errors = self._db.select(
                "error_log",
                columns="id,created_at",
            )

            for error in all_errors:
                created_str = error.get("created_at", "")
                if not created_str:
                    continue

                try:
                    created_at = datetime.fromisoformat(
                        created_str.replace("Z", "+00:00")
                    )
                except (ValueError, AttributeError):
                    continue

                if created_at < cutoff:
                    try:
                        self._db.delete("error_log", {"id": error["id"]})
                        count += 1
                    except DatabaseError as exc:
                        logger.error("Failed to delete old error_log %s: %s", error["id"], exc)

        except DatabaseError as exc:
            logger.error("Failed to fetch error_log entries for purge: %s", exc)

        if count > 0:
            logger.info("Purged %d error_log entries (>%d days old)", count, ERROR_LOG_RETENTION_DAYS)
        return count

    def purge_pipeline_runs(self) -> int:
        """
        Delete pipeline_runs entries older than 180 days.

        Returns:
            Number of entries deleted.
        """
        cutoff = datetime.now(timezone.utc) - timedelta(days=PIPELINE_RUNS_RETENTION_DAYS)
        count = 0

        try:
            all_runs = self._db.select(
                "pipeline_runs",
                columns="id,started_at",
            )

            for run in all_runs:
                started_str = run.get("started_at", "")
                if not started_str:
                    continue

                try:
                    started_at = datetime.fromisoformat(
                        started_str.replace("Z", "+00:00")
                    )
                except (ValueError, AttributeError):
                    continue

                if started_at < cutoff:
                    try:
                        self._db.delete("pipeline_runs", {"id": run["id"]})
                        count += 1
                    except DatabaseError as exc:
                        logger.error("Failed to delete old pipeline_run %s: %s", run["id"], exc)

        except DatabaseError as exc:
            logger.error("Failed to fetch pipeline_runs for purge: %s", exc)

        if count > 0:
            logger.info("Purged %d pipeline_runs entries (>%d days old)", count, PIPELINE_RUNS_RETENTION_DAYS)
        return count

    def archive_price_history(self) -> int:
        """
        Archive price_history entries older than 1 year to R2 as CSV,
        then delete from database.

        Returns:
            Number of entries archived and deleted.
        """
        cutoff = datetime.now(timezone.utc) - timedelta(days=PRICE_HISTORY_RETENTION_DAYS)
        count = 0

        try:
            all_history = self._db.select(
                "price_history",
                columns="*",
                order_by="changed_at",
            )

            old_entries: List[Dict[str, Any]] = []
            for entry in all_history:
                changed_str = entry.get("changed_at", "")
                if not changed_str:
                    continue

                try:
                    changed_at = datetime.fromisoformat(
                        changed_str.replace("Z", "+00:00")
                    )
                except (ValueError, AttributeError):
                    continue

                if changed_at < cutoff:
                    old_entries.append(entry)

            if not old_entries:
                logger.info("No price_history entries eligible for archival")
                return 0

            # Export to CSV
            csv_data = self._entries_to_csv(old_entries)

            # Upload to R2
            if self._storage and csv_data:
                date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                key = f"archives/price_history/price-history-{date_str}.csv"
                try:
                    self._storage.upload_backup(key, csv_data.encode("utf-8"))
                    logger.info("Uploaded price_history archive to R2: %s", key)
                except StorageError as exc:
                    logger.error("Failed to upload price_history archive: %s", exc)
                    return 0  # Don't delete if archive upload failed

            # Delete archived entries from database
            for entry in old_entries:
                try:
                    self._db.delete("price_history", {"id": entry["id"]})
                    count += 1
                except DatabaseError as exc:
                    logger.error("Failed to delete price_history %s: %s", entry["id"], exc)

        except DatabaseError as exc:
            logger.error("Failed to fetch price_history for archival: %s", exc)

        if count > 0:
            logger.info(
                "Archived and deleted %d price_history entries (>%d days old)",
                count, PRICE_HISTORY_RETENTION_DAYS,
            )
        return count

    @staticmethod
    def _entries_to_csv(entries: List[Dict[str, Any]]) -> str:
        """Convert a list of dicts to a CSV string."""
        if not entries:
            return ""

        output = io.StringIO()
        fieldnames = list(entries[0].keys())
        writer = csv.DictWriter(output, fieldnames=fieldnames)
        writer.writeheader()
        for entry in entries:
            writer.writerow(entry)

        return output.getvalue()

    def run_all(self) -> Dict[str, int]:
        """
        Run all purge operations.

        Returns:
            Dict with counts for each purge operation.
        """
        results = {
            "pii_purged": self.purge_pii(),
            "error_logs_purged": self.purge_error_logs(),
            "pipeline_runs_purged": self.purge_pipeline_runs(),
            "price_history_archived": self.archive_price_history(),
        }
        logger.info("PII purge complete: %s", results)
        return results
