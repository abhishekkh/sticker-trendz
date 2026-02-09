"""
Integration tests for RedditSource.

Requires REDDIT_CLIENT_ID and REDDIT_CLIENT_SECRET environment variables.
Read-only: fetches trending posts from a single subreddit.
"""

from __future__ import annotations

import pytest

from tests.conftest import skip_if_no_reddit
from src.trends.sources.reddit import RedditSource

pytestmark = [pytest.mark.integration, skip_if_no_reddit]


class TestRedditSourceIntegration:
    """Live Reddit API integration tests."""

    def test_client_initializes(self):
        """RedditSource initializes with real credentials."""
        source = RedditSource(subreddits=["memes"])
        assert source._reddit is not None

    def test_fetch_trends_returns_results(self):
        """fetch_trends() returns a non-empty list from r/memes."""
        source = RedditSource(subreddits=["memes"])
        trends = source.fetch_trends(posts_per_sub=5)
        assert isinstance(trends, list)
        assert len(trends) > 0

    def test_trend_items_have_required_fields(self):
        """Each trend item has topic, keywords, source=='reddit', and source_data with score."""
        source = RedditSource(subreddits=["memes"])
        trends = source.fetch_trends(posts_per_sub=5)

        assert len(trends) > 0
        for item in trends[:3]:
            assert "topic" in item
            assert isinstance(item["topic"], str)
            assert len(item["topic"]) > 0
            assert "keywords" in item
            assert isinstance(item["keywords"], list)
            assert item["source"] == "reddit"
            assert "source_data" in item
            assert "score" in item["source_data"]
            assert isinstance(item["source_data"]["score"], (int, float))

    def test_source_data_contains_subreddit(self):
        """source_data includes the subreddit name."""
        source = RedditSource(subreddits=["funny"])
        trends = source.fetch_trends(posts_per_sub=3)

        if not trends:
            pytest.skip("No trends returned from r/funny")

        for item in trends[:3]:
            assert item["source_data"]["subreddit"] == "funny"

    def test_single_subreddit_limits_posts(self):
        """posts_per_sub=3 limits results from a single subreddit."""
        source = RedditSource(subreddits=["memes"])
        trends = source.fetch_trends(posts_per_sub=3)
        # May get fewer if some posts have no keywords
        assert len(trends) <= 3
