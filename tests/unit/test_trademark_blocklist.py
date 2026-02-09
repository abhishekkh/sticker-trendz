"""
Unit tests for trademark and keyword blocklist matching (STR-043).

Covers check_trademark, check_keywords, and check_all from src/moderation/blocklist.
Uses the real data/ blocklist files; clear_cache() is used where needed to avoid
cross-test cache effects.
"""

import pytest

from src.moderation.blocklist import (
    check_trademark,
    check_keywords,
    check_all,
    clear_cache,
    get_trademark_blocklist,
    get_keyword_blocklist,
)


@pytest.fixture(autouse=True)
def _clear_blocklist_cache():
    """Clear blocklist caches before each test for consistent behavior."""
    clear_cache()
    yield
    clear_cache()


class TestCheckTrademark:
    """Tests for check_trademark(): known brands, clean topics, case and partial matches."""

    def test_mickey_mouse_detected_as_trademark(self):
        """'Mickey Mouse' is detected as trademark."""
        blocked, match = check_trademark("Mickey Mouse")
        assert blocked is True
        assert match == "mickey mouse"

    def test_nike_shoes_detected_as_trademark(self):
        """'Nike shoes' is detected as trademark (partial match)."""
        blocked, match = check_trademark("Nike shoes")
        assert blocked is True
        assert match == "nike"

    def test_baby_hippo_passes_trademark_check(self):
        """'baby hippo' passes trademark check (no match)."""
        blocked, match = check_trademark("baby hippo")
        assert blocked is False
        assert match is None

    def test_case_insensitivity_disney(self):
        """Case insensitivity: 'DISNEY' matches blocklist 'Disney'."""
        blocked, match = check_trademark("DISNEY")
        assert blocked is True
        assert match == "disney"

    def test_partial_match_spider_man_costume(self):
        """Partial match: 'Spider-Man costume' matches 'Spider-Man'."""
        blocked, match = check_trademark("Spider-Man costume")
        assert blocked is True
        assert match == "spider-man"

    def test_trademark_empty_string(self):
        """Empty string input returns not blocked."""
        blocked, match = check_trademark("")
        assert blocked is False
        assert match is None

    def test_trademark_very_long_input(self):
        """Very long input string is still checked (no crash); clean content passes."""
        long_prefix = "trending topic " * 500
        blocked, match = check_trademark(long_prefix + "baby hippo")
        assert blocked is False
        assert match is None

    def test_trademark_brand_in_long_text(self):
        """Trademark in a long string is still detected."""
        long_text = "Best " + "word " * 200 + "Nike shoes for sale"
        blocked, match = check_trademark(long_text)
        assert blocked is True
        assert match == "nike"


class TestCheckKeywords:
    """Tests for check_keywords(): offensive terms blocked, clean topics pass, edge cases."""

    def test_keyword_blocklist_blocks_offensive_term(self):
        """Keyword blocklist blocks offensive terms (e.g. racist)."""
        blocked, match = check_keywords("racist meme")
        assert blocked is True
        assert match == "racist"

    def test_clean_topic_passes_keyword_blocklist(self):
        """Clean topics pass keyword blocklist."""
        blocked, match = check_keywords("baby hippo sticker cute animal")
        assert blocked is False
        assert match is None

    def test_keyword_empty_string(self):
        """Empty string input returns not blocked."""
        blocked, match = check_keywords("")
        assert blocked is False
        assert match is None

    def test_keyword_very_long_input(self):
        """Very long input string is still checked; clean content passes."""
        long_text = "fun sticker design " * 500
        blocked, match = check_keywords(long_text)
        assert blocked is False
        assert match is None

    def test_keyword_word_boundary_short_entry(self):
        """Short keyword entries use word boundary (e.g. 'nazi' not in 'denazification')."""
        # Blocklist has 'nazi' (len 4); word-boundary matching should not match mid-word
        blocked, match = check_keywords("denazification")
        assert blocked is False
        assert match is None

    def test_keyword_standalone_short_entry_matches(self):
        """Standalone short blocklisted word is detected."""
        blocked, match = check_keywords("something nazi something")
        assert blocked is True
        assert match == "nazi"


class TestCheckAll:
    """Tests for check_all(): combined trademark and keyword with blocklist_type."""

    def test_check_all_trademark_returns_type(self):
        """check_all returns blocklist_type 'trademark' when trademark matches."""
        blocked, match, kind = check_all("Mickey Mouse sticker")
        assert blocked is True
        assert match == "mickey mouse"
        assert kind == "trademark"

    def test_check_all_keyword_returns_type(self):
        """check_all returns blocklist_type 'keyword' when keyword matches."""
        blocked, match, kind = check_all("racist content")
        assert blocked is True
        assert match == "racist"
        assert kind == "keyword"

    def test_check_all_clean_passes(self):
        """check_all returns not blocked for clean text."""
        blocked, match, kind = check_all("baby hippo cute sticker")
        assert blocked is False
        assert match is None
        assert kind is None

    def test_check_all_trademark_takes_precedence(self):
        """When both could match, trademark is checked first and returned."""
        # Use text that might contain both: e.g. a blocklisted brand name that
        # is also a word. Our blocklist has "nike" (trademark) and keyword list
        # has no "nike". So "Nike" text triggers trademark only.
        blocked, match, kind = check_all("Nike shoes")
        assert blocked is True
        assert kind == "trademark"
        assert match == "nike"


class TestBlocklistLoading:
    """Sanity checks that blocklists load from data files."""

    def test_trademark_blocklist_loads(self):
        """Trademark blocklist loads and contains expected entries."""
        entries = get_trademark_blocklist()
        assert isinstance(entries, list)
        assert "mickey mouse" in entries
        assert "nike" in entries
        assert "spider-man" in entries
        assert "disney" in entries

    def test_keyword_blocklist_loads(self):
        """Keyword blocklist loads and contains expected entries."""
        entries = get_keyword_blocklist()
        assert isinstance(entries, list)
        assert "racist" in entries
        assert "nazi" in entries
