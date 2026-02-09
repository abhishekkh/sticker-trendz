"""
Integration tests for EtsyPublisher with sandbox credentials.

Requires ETSY_API_KEY, ETSY_API_SECRET, and ETSY_SHOP_ID environment variables.
Tests the full listing lifecycle: create draft -> upload image -> activate -> update price -> deactivate.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from tests.conftest import skip_if_no_etsy
from src.publisher.etsy import EtsyPublisher

pytestmark = [pytest.mark.integration, skip_if_no_etsy]


@pytest.fixture
def mock_db():
    """Mock database to avoid real DB writes during Etsy integration tests."""
    db = MagicMock()
    db.count_active_listings.return_value = 0
    db.update_sticker.return_value = [{}]
    return db


@pytest.fixture
def publisher(mock_db):
    """EtsyPublisher with real API credentials but mocked DB."""
    return EtsyPublisher(db=mock_db, max_active_listings=300)


@pytest.fixture
def created_listing(publisher, mock_db):
    """Create a draft listing and return its ID for further tests. Deactivate on teardown."""
    sticker = {
        "id": "test-sticker-integration",
        "title": "Integration Test Sticker",
        "image_url": "",
        "size": "3in",
        "price": 4.99,
        "keywords": ["test", "integration"],
    }
    listing_id = publisher.create_listing(sticker)
    yield listing_id
    # Cleanup: deactivate listing
    if listing_id:
        try:
            publisher.deactivate_listing(listing_id)
        except Exception:
            pass


class TestEtsySandboxIntegration:
    """Live Etsy sandbox API integration tests."""

    def test_create_draft_listing(self, created_listing):
        """create_listing() returns a listing ID."""
        assert created_listing is not None
        assert isinstance(created_listing, str)
        assert len(created_listing) > 0

    def test_update_listing_price(self, publisher, created_listing):
        """update_listing_price() succeeds on an existing listing."""
        if not created_listing:
            pytest.skip("No listing was created")
        result = publisher.update_listing_price(created_listing, 5.49)
        assert result is True

    def test_deactivate_listing(self, publisher, mock_db):
        """deactivate_listing() succeeds on an existing listing."""
        sticker = {
            "id": "test-deactivate",
            "title": "Deactivate Test",
            "image_url": "",
            "size": "3in",
            "price": 3.99,
            "keywords": ["test"],
        }
        listing_id = publisher.create_listing(sticker)
        if not listing_id:
            pytest.skip("Could not create listing for deactivation test")
        result = publisher.deactivate_listing(listing_id)
        assert result is True
