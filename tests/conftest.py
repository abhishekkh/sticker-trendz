"""
Shared test fixtures and skip decorators for Sticker Trendz integration/e2e tests.

Credential checks return True only when the required environment variables are set.
Tests are skipped gracefully when credentials are missing.
"""

from __future__ import annotations

import logging
import os
import uuid
from typing import Optional

import pytest

from src.config import load_config

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Credential check helpers
# ---------------------------------------------------------------------------


def has_reddit_creds() -> bool:
    return bool(os.getenv("REDDIT_CLIENT_ID") and os.getenv("REDDIT_CLIENT_SECRET"))


def has_replicate_creds() -> bool:
    return bool(os.getenv("REPLICATE_API_TOKEN"))


def has_etsy_creds() -> bool:
    return bool(
        os.getenv("ETSY_API_KEY")
        and os.getenv("ETSY_API_SECRET")
        and os.getenv("ETSY_SHOP_ID")
    )


def has_supabase_creds() -> bool:
    return bool(os.getenv("SUPABASE_URL") and os.getenv("SUPABASE_SERVICE_KEY"))


def has_r2_creds() -> bool:
    return bool(
        os.getenv("CLOUDFLARE_R2_ACCESS_KEY")
        and os.getenv("CLOUDFLARE_R2_SECRET_KEY")
        and os.getenv("CLOUDFLARE_R2_BUCKET")
        and os.getenv("CLOUDFLARE_R2_ENDPOINT")
    )


# ---------------------------------------------------------------------------
# Skip decorators
# ---------------------------------------------------------------------------

skip_if_no_reddit = pytest.mark.skipif(
    not has_reddit_creds(), reason="Missing REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET"
)
skip_if_no_replicate = pytest.mark.skipif(
    not has_replicate_creds(), reason="Missing REPLICATE_API_TOKEN"
)
skip_if_no_etsy = pytest.mark.skipif(
    not has_etsy_creds(), reason="Missing ETSY_API_KEY / ETSY_API_SECRET / ETSY_SHOP_ID"
)
skip_if_no_supabase = pytest.mark.skipif(
    not has_supabase_creds(), reason="Missing SUPABASE_URL / SUPABASE_SERVICE_KEY"
)
skip_if_no_r2 = pytest.mark.skipif(
    not has_r2_creds(),
    reason="Missing CLOUDFLARE_R2_ACCESS_KEY / SECRET_KEY / BUCKET / ENDPOINT",
)

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def config():
    """Load config with partial credentials (won't fail on missing vars)."""
    return load_config(require_all=False)


@pytest.fixture
def test_prefix():
    """UUID-based prefix for data isolation in integration/e2e tests."""
    return f"TEST-{uuid.uuid4().hex[:8]}"


@pytest.fixture
def supabase_client():
    """Real Supabase client (skips if no credentials)."""
    if not has_supabase_creds():
        pytest.skip("No Supabase credentials")
    from src.db import SupabaseClient

    return SupabaseClient()


@pytest.fixture
def r2_storage(config):
    """Real R2 storage client (skips if no credentials)."""
    if not has_r2_creds():
        pytest.skip("No R2 credentials")
    from src.publisher.storage import R2StorageClient

    return R2StorageClient()


# ---------------------------------------------------------------------------
# Cleanup helpers
# ---------------------------------------------------------------------------


def cleanup_test_data(db, prefix: str) -> None:
    """Best-effort cleanup of test data from Supabase tables."""
    try:
        # Delete stickers first (foreign key to trends)
        stickers = db.select("stickers", filters={})
        for s in stickers:
            if prefix in (s.get("title") or ""):
                try:
                    db.delete("price_history", {"sticker_id": s["id"]})
                except Exception:
                    pass
                try:
                    db.delete("stickers", {"id": s["id"]})
                except Exception:
                    pass

        # Delete trends
        trends = db.select("trends", filters={})
        for t in trends:
            if prefix in (t.get("topic") or ""):
                try:
                    db.delete("trends", {"id": t["id"]})
                except Exception:
                    pass

        # Delete pipeline_runs
        runs = db.select("pipeline_runs", filters={})
        for r in runs:
            meta = r.get("metadata") or {}
            if prefix in str(meta):
                try:
                    db.delete("pipeline_runs", {"id": r["id"]})
                except Exception:
                    pass

        # Delete error_log entries
        errors = db.select("error_log", filters={})
        for e in errors:
            ctx = e.get("context") or {}
            if prefix in str(ctx):
                try:
                    db.delete("error_log", {"id": e["id"]})
                except Exception:
                    pass

    except Exception as exc:
        logger.warning("Cleanup failed for prefix '%s': %s", prefix, exc)


def cleanup_r2_objects(storage, prefix: str) -> None:
    """Best-effort cleanup of test objects from R2."""
    try:
        objects = storage.list_objects(prefix)
        for obj in objects:
            try:
                storage.delete_object(obj["Key"])
            except Exception:
                pass
    except Exception as exc:
        logger.warning("R2 cleanup failed for prefix '%s': %s", prefix, exc)
