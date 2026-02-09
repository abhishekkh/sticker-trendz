"""
Integration tests for SupabaseClient CRUD operations.

Requires SUPABASE_URL and SUPABASE_SERVICE_KEY environment variables.
All test records are prefixed with '[TEST-{uuid}]' for isolation.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from tests.conftest import skip_if_no_supabase, cleanup_test_data

pytestmark = [pytest.mark.integration, skip_if_no_supabase]


@pytest.fixture
def db():
    from src.db import SupabaseClient
    return SupabaseClient()


@pytest.fixture
def prefix():
    return f"TEST-{uuid.uuid4().hex[:8]}"


@pytest.fixture(autouse=True)
def cleanup(db, prefix):
    """Cleanup test data after each test."""
    yield
    cleanup_test_data(db, prefix)


class TestTrendsCRUD:
    """CRUD operations on the trends table."""

    def test_insert_trend(self, db, prefix):
        """Insert a trend and get it back."""
        now = datetime.now(timezone.utc).isoformat()
        data = {
            "topic": f"[{prefix}] Cute Robot",
            "topic_normalized": f"{prefix.lower()}-cute-robot",
            "source": "reddit",
            "keywords": ["robot", "cute"],
            "status": "discovered",
            "score_overall": 75.0,
            "created_at": now,
        }
        result = db.insert_trend(data)
        assert result.get("id")
        assert result["topic"] == data["topic"]
        assert result["status"] == "discovered"

    def test_get_trend_by_normalized_topic(self, db, prefix):
        """Retrieve a trend by its normalized topic."""
        normalized = f"{prefix.lower()}-test-lookup"
        db.insert_trend({
            "topic": f"[{prefix}] Test Lookup",
            "topic_normalized": normalized,
            "source": "google_trends",
            "keywords": ["test"],
            "status": "discovered",
            "score_overall": 50.0,
        })
        found = db.get_trend_by_normalized_topic(normalized)
        assert found is not None
        assert found["topic_normalized"] == normalized

    def test_update_trend(self, db, prefix):
        """Update a trend's status."""
        result = db.insert_trend({
            "topic": f"[{prefix}] Update Me",
            "topic_normalized": f"{prefix.lower()}-update-me",
            "source": "reddit",
            "keywords": [],
            "status": "discovered",
            "score_overall": 60.0,
        })
        trend_id = result["id"]
        db.update_trend(trend_id, {"status": "generated"})
        trends = db.select("trends", filters={"id": trend_id})
        assert trends[0]["status"] == "generated"


class TestStickersCRUD:
    """CRUD operations on the stickers table."""

    def test_insert_and_update_sticker(self, db, prefix):
        """Insert a sticker, then update its moderation_status."""
        # Need a trend first (foreign key)
        trend = db.insert_trend({
            "topic": f"[{prefix}] Sticker Trend",
            "topic_normalized": f"{prefix.lower()}-sticker-trend",
            "source": "reddit",
            "keywords": [],
            "status": "generated",
            "score_overall": 80.0,
        })
        sticker = db.insert_sticker({
            "trend_id": trend["id"],
            "title": f"[{prefix}] Test Sticker",
            "image_url": "https://example.com/test.png",
            "size": "3in",
            "moderation_status": "pending",
        })
        assert sticker.get("id")

        db.update_sticker(sticker["id"], {"moderation_status": "approved"})
        rows = db.select("stickers", filters={"id": sticker["id"]})
        assert rows[0]["moderation_status"] == "approved"


class TestPipelineRunsCRUD:
    """CRUD operations on the pipeline_runs table."""

    def test_insert_and_update_pipeline_run(self, db, prefix):
        """Insert a pipeline run, then complete it."""
        now = datetime.now(timezone.utc).isoformat()
        run = db.insert_pipeline_run({
            "workflow": "test_workflow",
            "status": "started",
            "started_at": now,
            "metadata": {"test_prefix": prefix},
        })
        assert run.get("id")

        db.update_pipeline_run(run["id"], {
            "status": "completed",
            "ended_at": now,
        })
        rows = db.select("pipeline_runs", filters={"id": run["id"]})
        assert rows[0]["status"] == "completed"


class TestErrorLogCRUD:
    """CRUD operations on the error_log table."""

    def test_insert_error(self, db, prefix):
        """Insert an error log entry."""
        error = db.insert_error({
            "workflow": "test_workflow",
            "step": "test_step",
            "error_type": "test_error",
            "error_message": f"[{prefix}] test error message",
            "resolved": False,
            "context": {"test_prefix": prefix},
        })
        assert error.get("id")

    def test_get_recent_errors(self, db, prefix):
        """Insert and retrieve recent errors by workflow."""
        db.insert_error({
            "workflow": f"test_{prefix}",
            "step": "test",
            "error_type": "test",
            "error_message": f"[{prefix}] error 1",
            "resolved": False,
            "context": {"test_prefix": prefix},
        })
        errors = db.get_recent_errors(f"test_{prefix}", limit=5)
        assert len(errors) >= 1
        assert prefix in errors[0]["error_message"]
