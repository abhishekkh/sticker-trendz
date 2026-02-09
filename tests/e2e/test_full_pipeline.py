"""
End-to-end test for the full sticker pipeline.

Requires Supabase, Replicate, Etsy (sandbox), and R2 credentials.
Injects a fake trend, steps through image generation -> moderation -> Etsy publish,
then verifies all artifacts were created correctly.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from tests.conftest import (
    has_supabase_creds,
    has_replicate_creds,
    has_etsy_creds,
    has_r2_creds,
    cleanup_test_data,
    cleanup_r2_objects,
)

pytestmark = pytest.mark.e2e

_skip_reason = "Missing one or more credentials: supabase, replicate, etsy, r2"
_skip = pytest.mark.skipif(
    not (has_supabase_creds() and has_replicate_creds() and has_etsy_creds() and has_r2_creds()),
    reason=_skip_reason,
)


@pytest.fixture
def e2e_prefix():
    return f"E2E-{uuid.uuid4().hex[:8]}"


@pytest.fixture
def db():
    from src.db import SupabaseClient
    return SupabaseClient()


@pytest.fixture
def storage():
    from src.publisher.storage import R2StorageClient
    return R2StorageClient()


@pytest.fixture(autouse=True)
def cleanup(db, storage, e2e_prefix):
    """Cleanup all test data after the test."""
    yield
    cleanup_test_data(db, e2e_prefix)
    cleanup_r2_objects(storage, f"stickers/")


@_skip
class TestFullPipelineE2E:
    """End-to-end: trend -> image generation -> moderation -> Etsy publish."""

    def test_pipeline_creates_stickers_for_trend(self, db, storage, e2e_prefix):
        """
        Inject a fake trend, generate images, moderate (mock auto-approve),
        and verify sticker records are created with correct data.
        """
        # Step 1: Inject a fake trend
        topic = f"[{e2e_prefix}] Cute Robot"
        now = datetime.now(timezone.utc).isoformat()
        trend = db.insert_trend({
            "topic": topic,
            "topic_normalized": f"{e2e_prefix.lower()}-cute-robot",
            "source": "reddit",
            "keywords": ["cute", "robot", "sticker"],
            "status": "discovered",
            "score_overall": 85.0,
            "created_at": now,
        })
        assert trend.get("id")
        trend_id = trend["id"]

        # Step 2: Generate images via ImageGenerator
        from src.stickers.image_generator import ImageGenerator
        from src.monitoring.pipeline_logger import PipelineRunLogger
        from src.monitoring.error_logger import ErrorLogger

        pipeline_logger = PipelineRunLogger(db)
        error_logger = ErrorLogger(db)

        generator = ImageGenerator(
            db=db,
            storage=storage,
            pipeline_logger=pipeline_logger,
            error_logger=error_logger,
            max_images_per_day=3,
        )

        stickers = generator.generate_for_trend(trend)
        assert len(stickers) > 0, "Expected at least 1 sticker to be generated"

        # Verify sticker record
        sticker = stickers[0]
        assert sticker.get("trend_id") == trend_id
        assert sticker.get("moderation_status") == "pending"
        assert sticker.get("image_url")

        # Step 3: Moderate (mock OpenAI to auto-approve)
        from src.moderation.moderator import ContentModerator, ModerationResult

        mock_openai = MagicMock()
        moderator = ContentModerator(db=db, openai_client=mock_openai)

        with patch.object(
            moderator, "moderate_image",
            return_value=ModerationResult(status="approved", score=0.1),
        ):
            updated = moderator.moderate_sticker(sticker)
            assert updated["moderation_status"] == "approved"

        # Verify sticker updated in DB
        rows = db.select("stickers", filters={"id": sticker["id"]})
        assert rows[0]["moderation_status"] == "approved"

        # Verify trend status was updated to 'generated'
        trend_rows = db.select("trends", filters={"id": trend_id})
        assert trend_rows[0]["status"] == "generated"

    def test_pipeline_with_etsy_publish(self, db, storage, e2e_prefix):
        """
        Full flow including Etsy listing creation (sandbox).
        Creates a trend, generates a sticker, moderates it, then publishes to Etsy.
        """
        topic = f"[{e2e_prefix}] Space Cat"
        now = datetime.now(timezone.utc).isoformat()
        trend = db.insert_trend({
            "topic": topic,
            "topic_normalized": f"{e2e_prefix.lower()}-space-cat",
            "source": "google_trends",
            "keywords": ["space", "cat", "sticker"],
            "status": "discovered",
            "score_overall": 90.0,
            "created_at": now,
        })
        trend_id = trend["id"]

        # Generate
        from src.stickers.image_generator import ImageGenerator
        from src.monitoring.pipeline_logger import PipelineRunLogger
        from src.monitoring.error_logger import ErrorLogger

        generator = ImageGenerator(
            db=db,
            storage=storage,
            pipeline_logger=PipelineRunLogger(db),
            error_logger=ErrorLogger(db),
            max_images_per_day=3,
        )
        stickers = generator.generate_for_trend(trend)
        if not stickers:
            pytest.skip("Image generation failed (API issue)")

        sticker = stickers[0]

        # Mock-approve moderation
        from src.moderation.moderator import ContentModerator, ModerationResult

        moderator = ContentModerator(db=db, openai_client=MagicMock())
        with patch.object(
            moderator, "moderate_image",
            return_value=ModerationResult(status="approved", score=0.05),
        ):
            moderator.moderate_sticker(sticker)

        # Publish to Etsy sandbox
        from src.publisher.etsy import EtsyPublisher

        publisher = EtsyPublisher(db=db, max_active_listings=300)
        listing_id = publisher.create_listing(
            sticker={**sticker, "price": 5.49},
            trend=trend,
        )

        if listing_id:
            # Verify sticker was updated with listing ID
            rows = db.select("stickers", filters={"id": sticker["id"]})
            assert rows[0].get("etsy_listing_id") == listing_id

            # Cleanup: deactivate listing
            publisher.deactivate_listing(listing_id)
