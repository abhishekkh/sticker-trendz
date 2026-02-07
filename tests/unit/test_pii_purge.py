"""Tests for src/analytics/pii_purge.py -- PII purge and data retention compliance."""

from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch, call
import pytest

from src.analytics.pii_purge import PIIPurger, PII_RETENTION_DAYS, ERROR_LOG_RETENTION_DAYS, PIPELINE_RUNS_RETENTION_DAYS, PRICE_HISTORY_RETENTION_DAYS
from src.db import DatabaseError
from src.publisher.storage import StorageError


class TestPIIPurger:
    """Tests for the PIIPurger class."""

    def test_purge_pii_sets_customer_data_to_null_for_old_delivered_orders(self):
        """Orders delivered 90+ days ago should have customer_data set to NULL."""
        mock_db = MagicMock()
        mock_storage = MagicMock()
        purger = PIIPurger(db=mock_db, storage=mock_storage)

        cutoff = datetime.now(timezone.utc) - timedelta(days=PII_RETENTION_DAYS)
        old_delivered = (cutoff - timedelta(days=1)).isoformat()

        mock_db.select.return_value = [
            {
                "id": "order-1",
                "status": "delivered",
                "delivered_at": old_delivered,
                "customer_data": {"name": "John Doe", "email": "john@example.com"},
            },
        ]

        count = purger.purge_pii()

        assert count == 1
        mock_db.update_order.assert_called_once_with("order-1", {"customer_data": None})

    def test_purge_pii_does_not_purge_orders_delivered_less_than_90_days_ago(self):
        """Orders delivered 89 days ago should NOT be purged."""
        mock_db = MagicMock()
        purger = PIIPurger(db=mock_db)

        cutoff = datetime.now(timezone.utc) - timedelta(days=PII_RETENTION_DAYS)
        recent_delivered = (cutoff + timedelta(days=1)).isoformat()

        mock_db.select.return_value = [
            {
                "id": "order-2",
                "status": "delivered",
                "delivered_at": recent_delivered,
                "customer_data": {"name": "Jane Doe"},
            },
        ]

        count = purger.purge_pii()

        assert count == 0
        mock_db.update_order.assert_not_called()

    def test_purge_pii_skips_orders_with_null_customer_data(self):
        """Orders with customer_data already NULL should be skipped."""
        mock_db = MagicMock()
        purger = PIIPurger(db=mock_db)

        cutoff = datetime.now(timezone.utc) - timedelta(days=PII_RETENTION_DAYS)
        old_delivered = (cutoff - timedelta(days=1)).isoformat()

        mock_db.select.return_value = [
            {
                "id": "order-3",
                "status": "delivered",
                "delivered_at": old_delivered,
                "customer_data": None,
            },
        ]

        count = purger.purge_pii()

        assert count == 0
        mock_db.update_order.assert_not_called()

    def test_purge_pii_handles_database_error_gracefully(self):
        """Database errors during PII purge should be logged but not crash."""
        mock_db = MagicMock()
        purger = PIIPurger(db=mock_db)

        mock_db.select.side_effect = DatabaseError("Connection failed")

        count = purger.purge_pii()

        assert count == 0

    def test_purge_pii_continues_on_individual_update_failure(self):
        """If one update fails, purging should continue for other orders."""
        mock_db = MagicMock()
        purger = PIIPurger(db=mock_db)

        cutoff = datetime.now(timezone.utc) - timedelta(days=PII_RETENTION_DAYS)
        old_delivered = (cutoff - timedelta(days=1)).isoformat()

        mock_db.select.return_value = [
            {"id": "order-1", "delivered_at": old_delivered, "customer_data": {"name": "A"}},
            {"id": "order-2", "delivered_at": old_delivered, "customer_data": {"name": "B"}},
        ]

        # First update fails, second succeeds
        mock_db.update_order.side_effect = [DatabaseError("Failed"), None]

        count = purger.purge_pii()

        assert count == 1
        assert mock_db.update_order.call_count == 2

    def test_purge_error_logs_deletes_entries_older_than_90_days(self):
        """Error log entries older than 90 days should be deleted."""
        mock_db = MagicMock()
        purger = PIIPurger(db=mock_db)

        cutoff = datetime.now(timezone.utc) - timedelta(days=ERROR_LOG_RETENTION_DAYS)
        old_error = (cutoff - timedelta(days=1)).isoformat()

        mock_db.select.return_value = [
            {"id": "error-1", "created_at": old_error},
        ]

        count = purger.purge_error_logs()

        assert count == 1
        mock_db.delete.assert_called_once_with("error_log", {"id": "error-1"})

    def test_purge_error_logs_does_not_delete_recent_entries(self):
        """Error log entries less than 90 days old should NOT be deleted."""
        mock_db = MagicMock()
        purger = PIIPurger(db=mock_db)

        cutoff = datetime.now(timezone.utc) - timedelta(days=ERROR_LOG_RETENTION_DAYS)
        recent_error = (cutoff + timedelta(days=1)).isoformat()

        mock_db.select.return_value = [
            {"id": "error-2", "created_at": recent_error},
        ]

        count = purger.purge_error_logs()

        assert count == 0
        mock_db.delete.assert_not_called()

    def test_purge_pipeline_runs_deletes_entries_older_than_180_days(self):
        """Pipeline run entries older than 180 days should be deleted."""
        mock_db = MagicMock()
        purger = PIIPurger(db=mock_db)

        cutoff = datetime.now(timezone.utc) - timedelta(days=PIPELINE_RUNS_RETENTION_DAYS)
        old_run = (cutoff - timedelta(days=1)).isoformat()

        mock_db.select.return_value = [
            {"id": "run-1", "started_at": old_run},
        ]

        count = purger.purge_pipeline_runs()

        assert count == 1
        mock_db.delete.assert_called_once_with("pipeline_runs", {"id": "run-1"})

    def test_purge_pipeline_runs_does_not_delete_recent_entries(self):
        """Pipeline run entries less than 180 days old should NOT be deleted."""
        mock_db = MagicMock()
        purger = PIIPurger(db=mock_db)

        cutoff = datetime.now(timezone.utc) - timedelta(days=PIPELINE_RUNS_RETENTION_DAYS)
        recent_run = (cutoff + timedelta(days=1)).isoformat()

        mock_db.select.return_value = [
            {"id": "run-2", "started_at": recent_run},
        ]

        count = purger.purge_pipeline_runs()

        assert count == 0
        mock_db.delete.assert_not_called()

    def test_archive_price_history_uploads_to_r2_then_deletes_from_db(self):
        """Price history older than 1 year should be archived to R2 then deleted."""
        mock_db = MagicMock()
        mock_storage = MagicMock()
        purger = PIIPurger(db=mock_db, storage=mock_storage)

        cutoff = datetime.now(timezone.utc) - timedelta(days=PRICE_HISTORY_RETENTION_DAYS)
        old_price_change = (cutoff - timedelta(days=1)).isoformat()

        mock_db.select.return_value = [
            {
                "id": "price-1",
                "sticker_id": "sticker-123",
                "old_price": "5.49",
                "new_price": "4.49",
                "changed_at": old_price_change,
            },
        ]

        count = purger.archive_price_history()

        assert count == 1
        # Verify upload to R2 was called
        assert mock_storage.upload_backup.called
        upload_call = mock_storage.upload_backup.call_args
        assert "price-history" in upload_call[0][0]
        assert b"price-1" in upload_call[0][1] or "price-1" in upload_call[0][1].decode()
        # Verify delete from DB was called
        mock_db.delete.assert_called_once_with("price_history", {"id": "price-1"})

    def test_archive_price_history_does_not_archive_recent_entries(self):
        """Price history less than 1 year old should NOT be archived."""
        mock_db = MagicMock()
        mock_storage = MagicMock()
        purger = PIIPurger(db=mock_db, storage=mock_storage)

        cutoff = datetime.now(timezone.utc) - timedelta(days=PRICE_HISTORY_RETENTION_DAYS)
        recent_change = (cutoff + timedelta(days=1)).isoformat()

        mock_db.select.return_value = [
            {"id": "price-2", "changed_at": recent_change},
        ]

        count = purger.archive_price_history()

        assert count == 0
        mock_storage.upload_backup.assert_not_called()
        mock_db.delete.assert_not_called()

    def test_archive_price_history_does_not_delete_if_upload_fails(self):
        """If R2 upload fails, entries should NOT be deleted from DB."""
        mock_db = MagicMock()
        mock_storage = MagicMock()
        purger = PIIPurger(db=mock_db, storage=mock_storage)

        cutoff = datetime.now(timezone.utc) - timedelta(days=PRICE_HISTORY_RETENTION_DAYS)
        old_change = (cutoff - timedelta(days=1)).isoformat()

        mock_db.select.return_value = [
            {"id": "price-3", "changed_at": old_change},
        ]

        mock_storage.upload_backup.side_effect = StorageError("Upload failed")

        count = purger.archive_price_history()

        assert count == 0
        mock_db.delete.assert_not_called()

    def test_archive_price_history_exports_csv_with_correct_format(self):
        """CSV export should include all fields and proper headers."""
        mock_db = MagicMock()
        mock_storage = MagicMock()
        purger = PIIPurger(db=mock_db, storage=mock_storage)

        cutoff = datetime.now(timezone.utc) - timedelta(days=PRICE_HISTORY_RETENTION_DAYS)
        old_change = (cutoff - timedelta(days=1)).isoformat()

        mock_db.select.return_value = [
            {
                "id": "price-1",
                "sticker_id": "sticker-123",
                "old_price": "5.49",
                "new_price": "4.49",
                "pricing_tier": "trending",
                "reason": "age_decay",
                "changed_at": old_change,
            },
        ]

        purger.archive_price_history()

        upload_call = mock_storage.upload_backup.call_args
        csv_content = upload_call[0][1].decode()

        # Verify CSV has headers
        assert "id" in csv_content
        assert "sticker_id" in csv_content
        assert "old_price" in csv_content
        # Verify data row
        assert "price-1" in csv_content
        assert "sticker-123" in csv_content

    def test_run_all_executes_all_purge_operations(self):
        """run_all should execute all four purge operations and return counts."""
        mock_db = MagicMock()
        mock_storage = MagicMock()
        purger = PIIPurger(db=mock_db, storage=mock_storage)

        # Mock all operations to return 0 to avoid complex setup
        mock_db.select.return_value = []

        results = purger.run_all()

        assert "pii_purged" in results
        assert "error_logs_purged" in results
        assert "pipeline_runs_purged" in results
        assert "price_history_archived" in results
        assert all(isinstance(v, int) for v in results.values())

    def test_entries_to_csv_creates_valid_csv(self):
        """_entries_to_csv should create properly formatted CSV."""
        entries = [
            {"id": "1", "name": "Alice", "age": 30},
            {"id": "2", "name": "Bob", "age": 25},
        ]

        csv_str = PIIPurger._entries_to_csv(entries)

        assert "id,name,age" in csv_str
        assert "1,Alice,30" in csv_str
        assert "2,Bob,25" in csv_str

    def test_entries_to_csv_handles_empty_list(self):
        """_entries_to_csv should return empty string for empty list."""
        csv_str = PIIPurger._entries_to_csv([])
        assert csv_str == ""
