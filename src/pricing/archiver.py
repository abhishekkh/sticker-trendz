"""
Sticker archiver for Sticker Trendz.

Deactivates Etsy listings for stickers with 0 sales and 0 views for
14+ days. Updates sticker status to 'archived' and logs to price_history.
Runs as part of the daily pricing engine before new listings are created,
freeing Etsy listing slots (max 300 active).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

from src.db import SupabaseClient, DatabaseError
from src.publisher.etsy import EtsyPublisher
from src.monitoring.error_logger import ErrorLogger

logger = logging.getLogger(__name__)

# Stickers with 0 sales and 0 views for this many days get archived
ARCHIVE_THRESHOLD_DAYS = 14


class StickerArchiver:
    """
    Archives stale stickers by deactivating their Etsy listings.

    A sticker is considered stale and archivable if it has:
      - 0 sales (sales_count = 0)
      - 0 views (view_count = 0)
      - Both conditions for 14+ days since publishing
    """

    def __init__(
        self,
        db: Optional[SupabaseClient] = None,
        etsy_publisher: Optional[EtsyPublisher] = None,
        error_logger: Optional[ErrorLogger] = None,
        threshold_days: int = ARCHIVE_THRESHOLD_DAYS,
    ) -> None:
        self._db = db or SupabaseClient()
        self._publisher = etsy_publisher
        self._error_logger = error_logger
        self._threshold_days = threshold_days

    def get_archivable_stickers(self) -> List[Dict[str, Any]]:
        """
        Find stickers eligible for archiving.

        Criteria:
          - Has an Etsy listing (etsy_listing_id is not null)
          - sales_count = 0
          - view_count = 0
          - published_at is at least ARCHIVE_THRESHOLD_DAYS ago

        Returns:
            List of sticker dicts matching archival criteria.
        """
        try:
            published = self._db.get_published_stickers()
        except DatabaseError as exc:
            logger.error("Failed to fetch published stickers for archival check: %s", exc)
            return []

        cutoff = datetime.now(timezone.utc) - timedelta(days=self._threshold_days)
        archivable: List[Dict[str, Any]] = []

        for sticker in published:
            # Skip already archived stickers
            if sticker.get("moderation_status") == "archived":
                continue

            # Check sales and views
            sales_count = sticker.get("sales_count", 0) or 0
            view_count = sticker.get("view_count", 0) or 0

            if sales_count > 0 or view_count > 0:
                continue

            # Check published_at age
            published_at_str = sticker.get("published_at", "")
            if not published_at_str:
                continue

            try:
                published_at = datetime.fromisoformat(
                    published_at_str.replace("Z", "+00:00")
                )
            except (ValueError, AttributeError):
                continue

            if published_at <= cutoff:
                archivable.append(sticker)

        logger.info(
            "Found %d archivable stickers (0 sales/views for %d+ days)",
            len(archivable), self._threshold_days,
        )
        return archivable

    def archive_sticker(self, sticker: Dict[str, Any]) -> bool:
        """
        Archive a single sticker: deactivate the Etsy listing and update status.

        Args:
            sticker: Sticker dict from Supabase.

        Returns:
            True if archived successfully, False otherwise.
        """
        sticker_id = sticker.get("id", "")
        listing_id = sticker.get("etsy_listing_id", "")
        old_price = float(sticker.get("price", 0))

        # Deactivate on Etsy
        if listing_id and self._publisher:
            success = self._publisher.deactivate_listing(listing_id)
            if not success:
                logger.warning(
                    "Failed to deactivate Etsy listing %s for sticker %s",
                    listing_id, sticker_id,
                )
                if self._error_logger:
                    self._error_logger.log_error(
                        workflow="pricing_engine",
                        step="archive",
                        error_type="api_error",
                        error_message=f"Failed to deactivate listing {listing_id}",
                        service="etsy",
                        context={"sticker_id": sticker_id, "listing_id": listing_id},
                    )
                return False

        # Update sticker status in Supabase
        try:
            self._db.update_sticker(sticker_id, {
                "moderation_status": "archived",
                "current_pricing_tier": "archived",
            })
        except DatabaseError as exc:
            logger.error("Failed to update sticker %s to archived: %s", sticker_id, exc)
            return False

        # Log to price_history
        try:
            self._db.insert_price_history({
                "sticker_id": sticker_id,
                "old_price": old_price,
                "new_price": 0,
                "pricing_tier": "archived",
                "reason": "archived",
            })
        except DatabaseError as exc:
            logger.warning("Failed to log price_history for archived sticker %s: %s", sticker_id, exc)

        logger.info("Archived sticker %s (listing %s)", sticker_id, listing_id)
        return True

    def run(self) -> int:
        """
        Run the archiver on all eligible stickers.

        Returns:
            Number of stickers successfully archived.
        """
        archivable = self.get_archivable_stickers()
        if not archivable:
            logger.info("No stickers to archive")
            return 0

        archived_count = 0
        for sticker in archivable:
            if self.archive_sticker(sticker):
                archived_count += 1

        logger.info("Archived %d/%d eligible stickers", archived_count, len(archivable))
        return archived_count
