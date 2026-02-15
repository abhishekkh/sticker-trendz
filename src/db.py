"""
Supabase client wrapper for Sticker Trendz.

Provides helper methods for CRUD operations on all major tables.
Initializes using SUPABASE_URL and SUPABASE_SERVICE_KEY.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from supabase import create_client, Client

from src.config import load_config, ConfigError

logger = logging.getLogger(__name__)

# Column whitelist per table to prevent injection via filter keys
_ALLOWED_COLUMNS: Dict[str, frozenset] = {
    "trends": frozenset({
        "id", "topic", "topic_normalized", "source", "sources", "keywords",
        "status", "score_overall", "score_velocity", "score_sentiment",
        "created_at", "updated_at",
    }),
    "stickers": frozenset({
        "id", "trend_id", "title", "description", "tags", "image_url",
        "thumbnail_url", "original_url", "size", "price", "floor_price",
        "moderation_status", "moderation_score", "moderation_categories",
        "current_pricing_tier", "etsy_listing_id", "published_at",
        "generation_prompt", "generation_model", "generation_model_version",
        "fulfillment_provider", "sales_count", "view_count", "last_sale_at",
        "created_at", "updated_at",
    }),
    "orders": frozenset({
        "id", "sticker_id", "etsy_receipt_id", "status", "quantity",
        "price_at_sale", "pricing_tier_at_sale", "created_at", "updated_at",
    }),
    "pipeline_runs": frozenset({
        "id", "workflow", "status", "started_at", "ended_at",
        "duration_seconds", "trends_found", "stickers_generated",
        "prices_updated", "stickers_archived", "errors_count",
        "etsy_api_calls_used", "ai_cost_estimate_usd", "metadata",
    }),
    "error_log": frozenset({
        "id", "workflow", "step", "error_type", "error_message", "service",
        "pipeline_run_id", "retry_count", "resolved", "context", "created_at",
    }),
    "etsy_tokens": frozenset({
        "id", "shop_id", "access_token", "refresh_token", "expires_at",
        "updated_at",
    }),
    "pricing_tiers": frozenset({
        "id", "tier", "min_trend_age_days", "max_trend_age_days",
        "price_single_small", "price_single_large",
    }),
    "shipping_rates": frozenset({
        "id", "product_type", "fulfillment_provider", "shipping_cost",
        "packaging_cost", "is_active",
    }),
    "price_history": frozenset({
        "id", "sticker_id", "old_price", "new_price", "pricing_tier",
        "reason", "created_at",
    }),
}


def _validate_filter_columns(table: str, filters: Dict[str, Any]) -> None:
    """Validate that filter column names are in the allowed whitelist."""
    allowed = _ALLOWED_COLUMNS.get(table)
    if allowed is None:
        return  # Unknown table, skip validation
    for col in filters:
        if col not in allowed:
            raise DatabaseError(
                f"Invalid column '{col}' for table '{table}'. "
                f"Allowed: {sorted(allowed)}"
            )


class DatabaseError(Exception):
    """Raised on database operation failures."""


class SupabaseClient:
    """
    Thin wrapper around the Supabase Python client.

    Provides typed helper methods for each table to keep calling code clean.
    """

    def __init__(
        self,
        url: Optional[str] = None,
        key: Optional[str] = None,
        client: Optional[Client] = None,
    ) -> None:
        if client:
            self._client = client
            return

        _url = url
        _key = key
        if not _url or not _key:
            try:
                cfg = load_config()
                _url = _url or cfg.supabase.url
                _key = _key or cfg.supabase.service_key
            except ConfigError as exc:
                raise DatabaseError(
                    "Cannot initialize Supabase client: missing credentials. "
                    "Set SUPABASE_URL and SUPABASE_SERVICE_KEY."
                ) from exc

        try:
            self._client = create_client(_url, _key)
            logger.info("Supabase client initialized for %s", _url)
        except Exception as exc:
            raise DatabaseError(
                f"Failed to initialize Supabase client: {exc}"
            ) from exc

    @property
    def client(self) -> Client:
        return self._client

    # ------------------------------------------------------------------
    # Generic helpers
    # ------------------------------------------------------------------

    def insert(self, table: str, data: Dict[str, Any]) -> Dict[str, Any]:
        """Insert a row and return the inserted record."""
        try:
            result = self._client.table(table).insert(data).execute()
            if result.data:
                return result.data[0]
            return {}
        except Exception as exc:
            logger.error("Insert into '%s' failed: %s", table, exc)
            raise DatabaseError(f"Insert into '{table}' failed: {exc}") from exc

    def upsert(self, table: str, data: Dict[str, Any]) -> Dict[str, Any]:
        """Upsert a row and return the record."""
        try:
            result = self._client.table(table).upsert(data).execute()
            if result.data:
                return result.data[0]
            return {}
        except Exception as exc:
            logger.error("Upsert into '%s' failed: %s", table, exc)
            raise DatabaseError(f"Upsert into '{table}' failed: {exc}") from exc

    def update(
        self, table: str, filters: Dict[str, Any], data: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        """Update rows matching filters. Returns updated records."""
        _validate_filter_columns(table, filters)
        try:
            query = self._client.table(table).update(data)
            for col, val in filters.items():
                query = query.eq(col, val)
            result = query.execute()
            return result.data or []
        except Exception as exc:
            logger.error("Update '%s' failed: %s", table, exc)
            raise DatabaseError(f"Update '{table}' failed: {exc}") from exc

    def select(
        self,
        table: str,
        columns: str = "*",
        filters: Optional[Dict[str, Any]] = None,
        order_by: Optional[str] = None,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Select rows from a table with optional filters, ordering, limit, and offset."""
        if filters:
            _validate_filter_columns(table, filters)
        try:
            query = self._client.table(table).select(columns)
            if filters:
                for col, val in filters.items():
                    query = query.eq(col, val)
            if order_by:
                desc = order_by.startswith("-")
                col = order_by.lstrip("-")
                query = query.order(col, desc=desc)
            if offset is not None and limit is not None:
                query = query.range(offset, offset + limit - 1)
            elif limit:
                query = query.limit(limit)
            result = query.execute()
            return result.data or []
        except Exception as exc:
            logger.error("Select from '%s' failed: %s", table, exc)
            raise DatabaseError(f"Select from '{table}' failed: {exc}") from exc

    def delete(self, table: str, filters: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Delete rows matching filters. Returns deleted records."""
        _validate_filter_columns(table, filters)
        try:
            query = self._client.table(table).delete()
            for col, val in filters.items():
                query = query.eq(col, val)
            result = query.execute()
            return result.data or []
        except Exception as exc:
            logger.error("Delete from '%s' failed: %s", table, exc)
            raise DatabaseError(f"Delete from '{table}' failed: {exc}") from exc

    def rpc(self, function_name: str, params: Optional[Dict[str, Any]] = None) -> Any:
        """Call a Supabase RPC (stored function)."""
        try:
            result = self._client.rpc(function_name, params or {}).execute()
            return result.data
        except Exception as exc:
            logger.error("RPC '%s' failed: %s", function_name, exc)
            raise DatabaseError(f"RPC '{function_name}' failed: {exc}") from exc

    # ------------------------------------------------------------------
    # Trends
    # ------------------------------------------------------------------

    def insert_trend(self, data: Dict[str, Any]) -> Dict[str, Any]:
        return self.insert("trends", data)

    def get_trend_by_normalized_topic(self, normalized: str) -> Optional[Dict[str, Any]]:
        rows = self.select("trends", filters={"topic_normalized": normalized}, limit=1)
        return rows[0] if rows else None

    def get_trends_by_status(self, status: str, limit: int = 100) -> List[Dict[str, Any]]:
        return self.select("trends", filters={"status": status}, order_by="-score_overall", limit=limit)

    def update_trend(self, trend_id: str, data: Dict[str, Any]) -> List[Dict[str, Any]]:
        data["updated_at"] = datetime.now(timezone.utc).isoformat()
        return self.update("trends", {"id": trend_id}, data)

    # ------------------------------------------------------------------
    # Stickers
    # ------------------------------------------------------------------

    def insert_sticker(self, data: Dict[str, Any]) -> Dict[str, Any]:
        return self.insert("stickers", data)

    def get_stickers_by_status(self, moderation_status: str, limit: int = 100) -> List[Dict[str, Any]]:
        return self.select("stickers", filters={"moderation_status": moderation_status}, limit=limit)

    def get_published_stickers(self) -> List[Dict[str, Any]]:
        """Get all stickers that have been published (have an Etsy listing)."""
        try:
            result = (
                self._client.table("stickers")
                .select("*, trends(*)")
                .not_.is_("published_at", "null")
                .execute()
            )
            return result.data or []
        except Exception as exc:
            logger.error("get_published_stickers failed: %s", exc)
            raise DatabaseError(f"get_published_stickers failed: {exc}") from exc

    def update_sticker(self, sticker_id: str, data: Dict[str, Any]) -> List[Dict[str, Any]]:
        return self.update("stickers", {"id": sticker_id}, data)

    def count_active_listings(self) -> int:
        """Count stickers with a non-null published_at and not archived."""
        try:
            result = (
                self._client.table("stickers")
                .select("id", count="exact")
                .not_.is_("etsy_listing_id", "null")
                .neq("moderation_status", "archived")
                .execute()
            )
            return result.count or 0
        except Exception as exc:
            logger.error("count_active_listings failed: %s", exc)
            return 0

    # ------------------------------------------------------------------
    # Orders
    # ------------------------------------------------------------------

    def insert_order(self, data: Dict[str, Any]) -> Dict[str, Any]:
        return self.insert("orders", data)

    def get_orders_by_status(self, status: str) -> List[Dict[str, Any]]:
        return self.select("orders", filters={"status": status})

    def update_order(self, order_id: str, data: Dict[str, Any]) -> List[Dict[str, Any]]:
        data["updated_at"] = datetime.now(timezone.utc).isoformat()
        return self.update("orders", {"id": order_id}, data)

    # ------------------------------------------------------------------
    # Pipeline Runs
    # ------------------------------------------------------------------

    def insert_pipeline_run(self, data: Dict[str, Any]) -> Dict[str, Any]:
        return self.insert("pipeline_runs", data)

    def update_pipeline_run(self, run_id: str, data: Dict[str, Any]) -> List[Dict[str, Any]]:
        return self.update("pipeline_runs", {"id": run_id}, data)

    # ------------------------------------------------------------------
    # Error Log
    # ------------------------------------------------------------------

    def insert_error(self, data: Dict[str, Any]) -> Dict[str, Any]:
        return self.insert("error_log", data)

    def get_recent_errors(self, workflow: str, limit: int = 10) -> List[Dict[str, Any]]:
        return self.select(
            "error_log",
            filters={"workflow": workflow},
            order_by="-created_at",
            limit=limit,
        )

    # ------------------------------------------------------------------
    # Etsy Tokens (with locking support)
    # ------------------------------------------------------------------

    def get_etsy_token(self, shop_id: str) -> Optional[Dict[str, Any]]:
        rows = self.select("etsy_tokens", filters={"shop_id": shop_id}, limit=1)
        return rows[0] if rows else None

    def update_etsy_token(self, shop_id: str, data: Dict[str, Any]) -> List[Dict[str, Any]]:
        data["updated_at"] = datetime.now(timezone.utc).isoformat()
        return self.update("etsy_tokens", {"shop_id": shop_id}, data)

    # ------------------------------------------------------------------
    # Pricing Tiers
    # ------------------------------------------------------------------

    def get_pricing_tiers(self) -> List[Dict[str, Any]]:
        return self.select("pricing_tiers", order_by="min_trend_age_days")

    def get_pricing_tier(self, tier: str) -> Optional[Dict[str, Any]]:
        rows = self.select("pricing_tiers", filters={"tier": tier}, limit=1)
        return rows[0] if rows else None

    # ------------------------------------------------------------------
    # Shipping Rates
    # ------------------------------------------------------------------

    def get_shipping_rate(
        self, product_type: str, fulfillment_provider: str
    ) -> Optional[Dict[str, Any]]:
        rows = self.select(
            "shipping_rates",
            filters={
                "product_type": product_type,
                "fulfillment_provider": fulfillment_provider,
                "is_active": True,
            },
            limit=1,
        )
        return rows[0] if rows else None

    # ------------------------------------------------------------------
    # Price History
    # ------------------------------------------------------------------

    def insert_price_history(self, data: Dict[str, Any]) -> Dict[str, Any]:
        return self.insert("price_history", data)
