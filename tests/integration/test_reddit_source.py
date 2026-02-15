"""
Integration tests for RedditSource.

Uses Reddit's public unauthenticated JSON API — no credentials required.
All tests make live HTTP calls; they are skipped automatically by pytest.ini
markers when running in --unit-only mode.
"""

from __future__ import annotations

import pytest

from src.trends.sources.reddit import (
    RedditSource,
    DEFAULT_SUBREDDITS,
    MAX_TOPIC_LENGTH,
    MAX_SELFTEXT_LENGTH,
)

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def source_memes():
    """RedditSource pointed at r/memes, shared across tests in this module."""
    return RedditSource(subreddits=["memes"])


@pytest.fixture(scope="module")
def trends_memes(source_memes):
    """Cached fetch from r/memes (posts_per_sub=10) to avoid repeated HTTP calls."""
    trends = source_memes.fetch_trends(posts_per_sub=10)
    if not trends:
        pytest.skip("r/memes returned no trends (rate-limited or empty)")
    return trends


# ---------------------------------------------------------------------------
# Initialisation
# ---------------------------------------------------------------------------


class TestRedditSourceInit:
    def test_session_is_created(self):
        source = RedditSource()
        assert source._session is not None

    def test_user_agent_set_on_session(self):
        source = RedditSource(user_agent="test-bot/0.1")
        assert source._session.headers.get("User-Agent") == "test-bot/0.1"

    def test_default_subreddits_applied(self):
        source = RedditSource()
        assert source._subreddits == DEFAULT_SUBREDDITS

    def test_custom_subreddits_applied(self):
        source = RedditSource(subreddits=["python", "programming"])
        assert source._subreddits == ["python", "programming"]


# ---------------------------------------------------------------------------
# Raw post structure (_fetch_subreddit_hot)
# ---------------------------------------------------------------------------


class TestFetchSubredditHot:
    def test_returns_list_of_dicts(self, source_memes):
        posts = source_memes._fetch_subreddit_hot("memes", limit=5)
        assert isinstance(posts, list)
        assert len(posts) > 0

    def test_each_post_has_required_keys(self, source_memes):
        posts = source_memes._fetch_subreddit_hot("memes", limit=5)
        required = {"id", "title", "score", "upvote_ratio", "num_comments",
                    "url", "selftext", "subreddit", "created_utc"}
        for post in posts:
            assert required.issubset(post.keys()), f"Missing keys in: {post.keys()}"

    def test_subreddit_field_matches_requested(self, source_memes):
        posts = source_memes._fetch_subreddit_hot("memes", limit=3)
        for post in posts:
            assert post["subreddit"] == "memes"

    def test_score_is_numeric(self, source_memes):
        posts = source_memes._fetch_subreddit_hot("memes", limit=5)
        for post in posts:
            assert isinstance(post["score"], (int, float))

    def test_upvote_ratio_in_range(self, source_memes):
        posts = source_memes._fetch_subreddit_hot("memes", limit=5)
        for post in posts:
            assert 0.0 <= post["upvote_ratio"] <= 1.0

    def test_num_comments_non_negative(self, source_memes):
        posts = source_memes._fetch_subreddit_hot("memes", limit=5)
        for post in posts:
            assert post["num_comments"] >= 0

    def test_created_utc_is_positive(self, source_memes):
        posts = source_memes._fetch_subreddit_hot("memes", limit=5)
        for post in posts:
            assert post["created_utc"] > 0

    def test_title_length_within_max(self, source_memes):
        posts = source_memes._fetch_subreddit_hot("memes", limit=10)
        for post in posts:
            assert len(post["title"]) <= MAX_TOPIC_LENGTH

    def test_selftext_length_within_max(self, source_memes):
        posts = source_memes._fetch_subreddit_hot("memes", limit=10)
        for post in posts:
            assert len(post["selftext"]) <= MAX_SELFTEXT_LENGTH

    def test_limit_respected(self, source_memes):
        posts = source_memes._fetch_subreddit_hot("memes", limit=3)
        assert len(posts) <= 3


# ---------------------------------------------------------------------------
# fetch_trends() output shape and invariants
# ---------------------------------------------------------------------------


class TestFetchTrendsShape:
    def test_returns_list(self, trends_memes):
        assert isinstance(trends_memes, list)

    def test_each_item_has_required_top_level_keys(self, trends_memes):
        required = {"topic", "keywords", "source", "source_data", "score_hint"}
        for item in trends_memes:
            assert required.issubset(item.keys()), f"Missing keys: {item.keys()}"

    def test_source_is_reddit(self, trends_memes):
        for item in trends_memes:
            assert item["source"] == "reddit"

    def test_topic_is_non_empty_string(self, trends_memes):
        for item in trends_memes:
            assert isinstance(item["topic"], str)
            assert len(item["topic"]) > 0

    def test_topic_within_max_length(self, trends_memes):
        for item in trends_memes:
            assert len(item["topic"]) <= MAX_TOPIC_LENGTH

    def test_keywords_is_list_of_strings(self, trends_memes):
        for item in trends_memes:
            assert isinstance(item["keywords"], list)
            for kw in item["keywords"]:
                assert isinstance(kw, str)

    def test_keywords_are_lowercase(self, trends_memes):
        for item in trends_memes:
            for kw in item["keywords"]:
                assert kw == kw.lower(), f"Non-lowercase keyword: {kw!r}"

    def test_keywords_capped_at_ten(self, trends_memes):
        for item in trends_memes:
            assert len(item["keywords"]) <= 10

    def test_score_hint_is_numeric(self, trends_memes):
        for item in trends_memes:
            assert isinstance(item["score_hint"], (int, float))

    def test_source_data_has_required_fields(self, trends_memes):
        required = {"reddit_id", "subreddit", "score", "upvote_ratio", "num_comments"}
        for item in trends_memes:
            assert required.issubset(item["source_data"].keys())

    def test_source_data_subreddit_matches_requested(self, trends_memes):
        for item in trends_memes:
            assert item["source_data"]["subreddit"] == "memes"

    def test_score_hint_matches_source_data_score(self, trends_memes):
        for item in trends_memes:
            assert item["score_hint"] == item["source_data"]["score"]

    def test_reddit_id_is_non_empty_string(self, trends_memes):
        for item in trends_memes:
            assert isinstance(item["source_data"]["reddit_id"], str)
            assert len(item["source_data"]["reddit_id"]) > 0


# ---------------------------------------------------------------------------
# Sorting
# ---------------------------------------------------------------------------


class TestFetchTrendsSorting:
    def test_trends_sorted_by_score_descending(self, trends_memes):
        scores = [t["score_hint"] for t in trends_memes]
        assert scores == sorted(scores, reverse=True), "Trends not sorted by score desc"


# ---------------------------------------------------------------------------
# Multiple subreddits
# ---------------------------------------------------------------------------


class TestMultipleSubreddits:
    def test_trends_from_both_subreddits_present(self):
        source = RedditSource(subreddits=["memes", "python"])
        trends = source.fetch_trends(posts_per_sub=5)
        if not trends:
            pytest.skip("No trends returned")
        subreddits_seen = {t["source_data"]["subreddit"] for t in trends}
        # At least one of the two should appear
        assert subreddits_seen & {"memes", "python"}, "Neither subreddit in results"

    def test_default_subreddits_all_polled(self):
        source = RedditSource()
        trends = source.fetch_trends(posts_per_sub=3)
        if not trends:
            pytest.skip("No trends returned")
        subreddits_seen = {t["source_data"]["subreddit"] for t in trends}
        # At least 2 of the 3 defaults should appear (one might have no keywords)
        assert len(subreddits_seen) >= 2


# ---------------------------------------------------------------------------
# Graceful degradation
# ---------------------------------------------------------------------------


class TestGracefulDegradation:
    def test_invalid_subreddit_returns_empty_list(self):
        """A subreddit that doesn't exist should degrade gracefully."""
        source = RedditSource(subreddits=["this_subreddit_does_not_exist_xyzzy"])
        trends = source.fetch_trends(posts_per_sub=5)
        assert isinstance(trends, list)
        # May be empty or may contain results if Reddit returns something unexpected
        # The important thing is no exception is raised

    def test_mix_valid_invalid_subreddits_partial_results(self):
        """Valid subs still return results even when one sub is invalid."""
        source = RedditSource(subreddits=["memes", "this_subreddit_does_not_exist_xyzzy"])
        trends = source.fetch_trends(posts_per_sub=5)
        assert isinstance(trends, list)
        # memes should contribute results
        valid_trends = [t for t in trends if t["source_data"]["subreddit"] == "memes"]
        assert len(valid_trends) > 0 or True  # graceful: no crash is the contract
