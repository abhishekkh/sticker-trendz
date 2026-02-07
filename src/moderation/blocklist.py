"""
Trademark and keyword blocklist matching for Sticker Trendz.

Loads blocklists from data/ files and provides matching functions that
check trend topics and sticker tags against both lists. Case-insensitive
matching with support for partial matches on multi-word terms.
"""

from __future__ import annotations

import logging
import os
import re
from functools import lru_cache
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TRADEMARK_BLOCKLIST_PATH = os.path.join(_PROJECT_ROOT, "data", "trademark_blocklist.txt")
KEYWORD_BLOCKLIST_PATH = os.path.join(_PROJECT_ROOT, "data", "keyword_blocklist.txt")


def _load_blocklist(filepath: str) -> List[str]:
    """
    Load a blocklist file, one entry per line.

    Strips whitespace, skips blank lines and comments (lines starting with #).
    """
    entries: List[str] = []
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    entries.append(line.lower())
        logger.info("Loaded %d entries from %s", len(entries), filepath)
    except FileNotFoundError:
        logger.warning("Blocklist file not found: %s", filepath)
    return entries


@lru_cache(maxsize=1)
def get_trademark_blocklist() -> List[str]:
    """Return the cached trademark blocklist entries (lowercase)."""
    return _load_blocklist(TRADEMARK_BLOCKLIST_PATH)


@lru_cache(maxsize=1)
def get_keyword_blocklist() -> List[str]:
    """Return the cached keyword blocklist entries (lowercase)."""
    return _load_blocklist(KEYWORD_BLOCKLIST_PATH)


def check_trademark(text: str) -> Tuple[bool, Optional[str]]:
    """
    Check whether the given text matches any entry in the trademark blocklist.

    Matching is case-insensitive and supports partial matches for multi-word
    blocklist entries (e.g. "Mickey Mouse" will match "mickey mouse costume").
    Also handles plurals by checking if the singular form is a substring.

    Args:
        text: The text to check (topic, title, tag, etc.).

    Returns:
        Tuple of (is_blocked, matched_term). matched_term is None if not blocked.
    """
    if not text:
        return False, None

    text_lower = text.lower()
    # Also check without trailing 's' for simple plural handling
    text_depluralized = text_lower.rstrip("s") if text_lower.endswith("s") else text_lower

    for entry in get_trademark_blocklist():
        # Check if the blocklist entry is a substring of the text
        if entry in text_lower:
            return True, entry
        # Check the depluralized form
        entry_depluralized = entry.rstrip("s") if entry.endswith("s") else entry
        if entry_depluralized in text_lower or entry in text_depluralized:
            return True, entry

    return False, None


def check_keywords(text: str) -> Tuple[bool, Optional[str]]:
    """
    Check whether the given text matches any entry in the keyword blocklist.

    Uses word-boundary matching to avoid false positives on substrings
    (e.g. "glass" should not match a blocklisted "ass").

    Args:
        text: The text to check.

    Returns:
        Tuple of (is_blocked, matched_term). matched_term is None if not blocked.
    """
    if not text:
        return False, None

    text_lower = text.lower()

    for entry in get_keyword_blocklist():
        # Use word boundary matching for short entries to avoid false positives
        if len(entry) <= 4:
            pattern = rf"\b{re.escape(entry)}\b"
            if re.search(pattern, text_lower):
                return True, entry
        else:
            # Longer entries can safely use substring matching
            if entry in text_lower:
                return True, entry

    return False, None


def check_all(text: str) -> Tuple[bool, Optional[str], Optional[str]]:
    """
    Check text against both trademark and keyword blocklists.

    Args:
        text: The text to check.

    Returns:
        Tuple of (is_blocked, matched_term, blocklist_type).
        blocklist_type is 'trademark' or 'keyword', or None if not blocked.
    """
    is_tm, tm_match = check_trademark(text)
    if is_tm:
        return True, tm_match, "trademark"

    is_kw, kw_match = check_keywords(text)
    if is_kw:
        return True, kw_match, "keyword"

    return False, None, None


def clear_cache() -> None:
    """Clear the cached blocklists (useful for testing)."""
    get_trademark_blocklist.cache_clear()
    get_keyword_blocklist.cache_clear()
