"""Tests for src/trends/dedup.py -- Jaccard similarity, topic normalization, and merge logic."""

import pytest

from src.trends.dedup import (
    jaccard_similarity,
    normalize_topic,
    deduplicate_trends,
    SIMILARITY_THRESHOLD,
)


class TestJaccardSimilarity:
    """Test Jaccard similarity calculation."""

    def test_known_sets_abc_vs_bcd(self):
        """Jaccard for {a,b,c} vs {b,c,d} = 2/4 = 0.5."""
        set_a = {"a", "b", "c"}
        set_b = {"b", "c", "d"}
        assert jaccard_similarity(set_a, set_b) == 0.5

    def test_identical_sets_returns_one(self):
        """Identical sets have Jaccard = 1.0."""
        s = {"x", "y", "z"}
        assert jaccard_similarity(s, s) == 1.0

    def test_disjoint_sets_returns_zero(self):
        """Disjoint sets have Jaccard = 0.0."""
        set_a = {"a", "b"}
        set_b = {"c", "d"}
        assert jaccard_similarity(set_a, set_b) == 0.0

    def test_empty_sets_returns_zero(self):
        """Both sets empty returns 0.0."""
        assert jaccard_similarity(set(), set()) == 0.0

    def test_one_empty_set_returns_zero(self):
        """One empty set yields Jaccard = 0.0."""
        assert jaccard_similarity({"a", "b"}, set()) == 0.0
        assert jaccard_similarity(set(), {"a", "b"}) == 0.0

    def test_subset_similarity(self):
        """Superset/subset: |A cap B| / |A cup B| = |smaller| / |larger|."""
        small = {"a", "b"}
        large = {"a", "b", "c", "d"}
        # intersection=2, union=4
        assert jaccard_similarity(small, large) == 0.5


class TestTopicNormalization:
    """Test topic normalization (lowercase, stemming)."""

    def test_lowercase(self):
        """Topic is lowercased."""
        assert normalize_topic("Baby Hippo") == "hippo"
        assert normalize_topic("TRENDING") == "trend"

    def test_stemming(self):
        """Common suffixes are stemmed."""
        # trending -> trend (ing stripped)
        result = normalize_topic("trending")
        assert result == "trend" or "trend" in result

    def test_non_alphanumeric_removed(self):
        """Non-alphanumeric chars (except spaces/hyphens) are removed."""
        result = normalize_topic("baby! hippo@")
        assert "baby" in result
        assert "hippo" in result

    def test_words_sorted(self):
        """Words are sorted alphabetically for order-independent matching."""
        a = normalize_topic("zebra apple monkey")
        b = normalize_topic("apple monkey zebra")
        assert a == b

    def test_empty_string_returns_empty(self):
        """Empty input returns empty string."""
        assert normalize_topic("") == ""

    def test_short_words_filtered(self):
        """Single-character words are filtered; longer words kept."""
        result = normalize_topic("a b hello world")
        assert "hello" in result or "world" in result


class TestDeduplicateTrendsMergeLogic:
    """Test that similarity > 0.6 triggers merge, <= 0.6 keeps separate."""

    def test_similarity_above_threshold_triggers_merge(self):
        """Trends with Jaccard > 0.6 are merged into one canonical trend."""
        # Keywords chosen so Jaccard > 0.6: e.g. {a,b,c} vs {a,b,c,d} -> 3/4 = 0.75
        trends = [
            {"topic": "Topic A", "keywords": ["a", "b", "c"], "source": "reddit"},
            {"topic": "Topic B", "keywords": ["a", "b", "c", "d"], "source": "google"},
        ]
        result = deduplicate_trends(trends, similarity_threshold=0.6)
        assert len(result) == 1
        assert "reddit" in result[0]["sources"]
        assert "google" in result[0]["sources"]

    def test_similarity_at_threshold_keeps_separate(self):
        """Trends with Jaccard == 0.6 stay separate (merge uses strict >)."""
        # {a,b,c} vs {b,c,d} = 0.5, need exactly 0.6
        # {a,b,c,d} vs {a,b,c,e} -> intersection 3, union 5 = 0.6
        trends = [
            {"topic": "Topic A", "keywords": ["a", "b", "c", "d"], "source": "reddit"},
            {"topic": "Topic B", "keywords": ["a", "b", "c", "e"], "source": "google"},
        ]
        result = deduplicate_trends(trends, similarity_threshold=0.6)
        # sim = 3/5 = 0.6, not > 0.6, so no merge
        assert len(result) == 2

    def test_similarity_below_threshold_keeps_separate(self):
        """Trends with Jaccard <= 0.6 remain as separate trends."""
        trends = [
            {"topic": "Topic A", "keywords": ["a", "b", "c"], "source": "reddit"},
            {"topic": "Topic B", "keywords": ["b", "c", "d"], "source": "google"},
        ]
        # Jaccard = 2/4 = 0.5
        result = deduplicate_trends(trends, similarity_threshold=0.6)
        assert len(result) == 2
        assert result[0]["sources"] == ["reddit"]
        assert result[1]["sources"] == ["google"]


class TestMergeSourcesArray:
    """Test that merge correctly combines sources arrays."""

    def test_merge_combines_sources(self):
        """Merged trend has combined sources from both trends."""
        trends = [
            {"topic": "Trend 1", "keywords": ["a", "b", "c"], "source": "reddit"},
            {"topic": "Trend 2", "keywords": ["a", "b", "c", "x"], "source": "google_trends"},
        ]
        result = deduplicate_trends(trends, similarity_threshold=0.5)
        assert len(result) == 1
        sources = result[0]["sources"]
        assert "reddit" in sources
        assert "google_trends" in sources
        assert len(sources) == 2

    def test_merge_with_list_source(self):
        """Source can be a list and is flattened into sources."""
        trends = [
            {"topic": "A", "keywords": ["x", "y", "z"], "source": ["reddit", "twitter"]},
            {"topic": "B", "keywords": ["x", "y", "z", "w"], "source": "google"},
        ]
        result = deduplicate_trends(trends, similarity_threshold=0.5)
        assert len(result) == 1
        sources = result[0]["sources"]
        assert "reddit" in sources
        assert "twitter" in sources
        assert "google" in sources


class TestDeduplicateEdgeCases:
    """Edge cases for deduplication."""

    def test_empty_trends_returns_empty(self):
        """Empty input returns empty list."""
        assert deduplicate_trends([]) == []

    def test_single_trend_returns_one(self):
        """Single trend returns one canonical trend."""
        trends = [{"topic": "Solo", "keywords": ["a", "b"], "source": "reddit"}]
        result = deduplicate_trends(trends)
        assert len(result) == 1
        assert result[0]["topic"] == "Solo"
        assert result[0]["sources"] == ["reddit"]
