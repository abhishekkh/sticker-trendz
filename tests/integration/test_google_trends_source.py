"""
Integration tests for GoogleTrendsSource.

No authentication required -- only needs pytrends library.
Rate limiting may cause empty results, which is acceptable.
"""

from __future__ import annotations

import pytest

from src.trends.sources.google_trends import GoogleTrendsSource


pytestmark = pytest.mark.integration


class TestGoogleTrendsSourceIntegration:
    """Live Google Trends API integration tests."""

    def test_client_initializes(self):
        """GoogleTrendsSource initializes without errors (pytrends optional)."""
        source = GoogleTrendsSource(max_requests=2)
        # _pytrends may be None if pytrends is not installed; the source degrades gracefully
        assert hasattr(source, "_pytrends")

    def test_fetch_trends_returns_list(self):
        """fetch_trends() returns a list (may be empty due to rate limiting)."""
        source = GoogleTrendsSource(max_requests=2)
        trends = source.fetch_trends()
        assert isinstance(trends, list)

    def test_trend_items_have_required_fields(self):
        """Each trend item has topic, keywords, and source=='google_trends'."""
        source = GoogleTrendsSource(max_requests=2)
        trends = source.fetch_trends()

        if not trends:
            pytest.skip("Google Trends returned empty results (rate limited)")

        for item in trends[:3]:
            assert "topic" in item
            assert isinstance(item["topic"], str)
            assert len(item["topic"]) > 0
            assert "keywords" in item
            assert isinstance(item["keywords"], list)
            assert item["source"] == "google_trends"

    def test_source_data_has_type_field(self):
        """source_data includes a 'type' field."""
        source = GoogleTrendsSource(max_requests=2)
        trends = source.fetch_trends()

        if not trends:
            pytest.skip("Google Trends returned empty results (rate limited)")

        for item in trends[:3]:
            assert "source_data" in item
            assert "type" in item["source_data"]
            assert item["source_data"]["type"] in ("trending_search", "realtime_trending")

    def test_request_count_respects_max(self):
        """Request count does not exceed max_requests."""
        source = GoogleTrendsSource(max_requests=1)
        source.fetch_trends()
        assert source._request_count <= 1

    def test_reset_request_count(self):
        """reset_request_count() clears the counter."""
        source = GoogleTrendsSource(max_requests=2)
        source.fetch_trends()
        source.reset_request_count()
        assert source._request_count == 0
