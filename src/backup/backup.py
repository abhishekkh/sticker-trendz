"""
Database backup module for Sticker Trendz.

Exports Supabase tables to JSON, compresses with gzip, uploads to
Cloudflare R2. Deletes backups older than 30 days. Runs daily as
part of the daily analytics workflow.
"""

from __future__ import annotations

import gzip
import json
import logging
import sys
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

from src.config import load_config, setup_logging
from src.db import SupabaseClient, DatabaseError
from src.publisher.storage import R2StorageClient, StorageError
from src.monitoring.pipeline_logger import PipelineRunLogger
from src.monitoring.error_logger import ErrorLogger
from src.monitoring.alerter import EmailAlerter

logger = logging.getLogger(__name__)

WORKFLOW_NAME = "db_backup"
BACKUP_PREFIX = "backups/db/"
BACKUP_RETENTION_DAYS = 30

# Tables to back up
BACKUP_TABLES = [
    "trends",
    "stickers",
    "orders",
    "pricing_tiers",
    "shipping_rates",
    "etsy_tokens",
    "pipeline_runs",
    "error_log",
    "price_history",
]


class DatabaseBackup:
    """
    Backs up Supabase database tables to Cloudflare R2.

    Exports each table as JSON, combines into a single document,
    compresses with gzip, and uploads to R2 with a dated key.
    Automatically cleans up backups older than 30 days.
    """

    def __init__(
        self,
        db: Optional[SupabaseClient] = None,
        storage: Optional[R2StorageClient] = None,
        pipeline_logger: Optional[PipelineRunLogger] = None,
        error_logger: Optional[ErrorLogger] = None,
        alerter: Optional[EmailAlerter] = None,
        retention_days: int = BACKUP_RETENTION_DAYS,
    ) -> None:
        self._db = db or SupabaseClient()
        self._storage = storage or R2StorageClient()
        self._pipeline_logger = pipeline_logger or PipelineRunLogger(self._db)
        self._error_logger = error_logger or ErrorLogger(self._db)
        self._alerter = alerter
        self._retention_days = retention_days

    def run_backup(self) -> bool:
        """
        Execute the full backup process.

        Steps:
          1. Export all tables to JSON
          2. Compress with gzip
          3. Upload to R2
          4. Clean up old backups
          5. Log success/failure

        Returns:
            True if the backup succeeded.
        """
        run_id = self._pipeline_logger.start_run(WORKFLOW_NAME)
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        backup_key = f"{BACKUP_PREFIX}sticker-trendz-{date_str}.json.gz"

        try:
            # Step 1: Export tables
            logger.info("Starting database backup for %s", date_str)
            backup_data = self._export_tables()

            # Step 2: Compress
            json_bytes = json.dumps(backup_data, default=str, indent=2).encode("utf-8")
            compressed = gzip.compress(json_bytes)

            logger.info(
                "Backup data: %d bytes uncompressed, %d bytes compressed (%.1f%% reduction)",
                len(json_bytes), len(compressed),
                (1 - len(compressed) / max(len(json_bytes), 1)) * 100,
            )

            # Step 3: Upload to R2
            self._storage.upload_backup(backup_key, compressed)
            logger.info("Backup uploaded to R2: %s", backup_key)

            # Step 4: Clean up old backups
            cleaned = self._cleanup_old_backups()

            # Step 5: Log success
            self._pipeline_logger.complete_run(
                run_id,
                metadata={
                    "backup_key": backup_key,
                    "backup_size_bytes": len(compressed),
                    "tables_backed_up": len(backup_data.get("tables", {})),
                    "old_backups_cleaned": cleaned,
                },
            )

            logger.info("Database backup completed successfully: %s", backup_key)
            return True

        except StorageError as exc:
            error_msg = f"R2 upload failed: {exc}"
            logger.error(error_msg)
            self._pipeline_logger.fail_run(run_id, error_message=error_msg)
            self._error_logger.log_error(
                workflow=WORKFLOW_NAME,
                step="upload",
                error_type="api_error",
                error_message=error_msg,
                service="r2",
                pipeline_run_id=run_id,
            )
            if self._alerter:
                self._alerter.send_alert(
                    "Database backup failed",
                    f"Failed to upload backup to R2: {exc}",
                )
            return False

        except DatabaseError as exc:
            error_msg = f"Database export failed: {exc}"
            logger.error(error_msg)
            self._pipeline_logger.fail_run(run_id, error_message=error_msg)
            self._error_logger.log_error(
                workflow=WORKFLOW_NAME,
                step="export",
                error_type="api_error",
                error_message=error_msg,
                service="supabase",
                pipeline_run_id=run_id,
            )
            if self._alerter:
                self._alerter.send_alert(
                    "Database backup failed",
                    f"Failed to export database: {exc}",
                )
            return False

        except Exception as exc:
            error_msg = f"Backup failed: {exc}"
            logger.error(error_msg)
            self._pipeline_logger.fail_run(run_id, error_message=error_msg)
            if self._alerter:
                self._alerter.send_alert("Database backup failed", error_msg)
            return False

    def _export_tables(self) -> Dict[str, Any]:
        """
        Export all configured tables from Supabase.

        Returns:
            Dict with 'timestamp', 'tables' (table_name -> rows list).
        """
        export: Dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "tables": {},
        }

        for table_name in BACKUP_TABLES:
            try:
                rows = self._db.select(table_name)
                export["tables"][table_name] = rows
                logger.info("Exported %d rows from '%s'", len(rows), table_name)
            except DatabaseError as exc:
                logger.error("Failed to export table '%s': %s", table_name, exc)
                export["tables"][table_name] = []

        total_rows = sum(len(rows) for rows in export["tables"].values())
        logger.info("Total rows exported: %d across %d tables", total_rows, len(BACKUP_TABLES))
        return export

    def _cleanup_old_backups(self) -> int:
        """
        Delete R2 backups older than the retention period.

        Returns:
            Number of old backups deleted.
        """
        cutoff = datetime.now(timezone.utc) - timedelta(days=self._retention_days)
        deleted = 0

        try:
            objects = self._storage.list_objects(BACKUP_PREFIX)
            for obj in objects:
                last_modified = obj.get("LastModified")
                if last_modified and last_modified < cutoff:
                    key = obj.get("Key", "")
                    try:
                        self._storage.delete_object(key)
                        deleted += 1
                        logger.info("Deleted old backup: %s", key)
                    except StorageError as exc:
                        logger.warning("Failed to delete old backup '%s': %s", key, exc)

        except StorageError as exc:
            logger.error("Failed to list backups for cleanup: %s", exc)

        if deleted > 0:
            logger.info("Cleaned up %d old backups (>%d days)", deleted, self._retention_days)
        return deleted


def main() -> None:
    """Entry point for `python -m src.backup.backup`."""
    setup_logging()
    logger.info("Starting database backup")

    try:
        load_config()
    except Exception as exc:
        logger.critical("Failed to load config: %s", exc)
        sys.exit(1)

    db = SupabaseClient()
    backup = DatabaseBackup(
        db=db,
        storage=R2StorageClient(),
        alerter=EmailAlerter(),
    )

    success = backup.run_backup()
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
