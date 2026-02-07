"""
Cross-source trend deduplication for Sticker Trendz.

Uses keyword normalization and Jaccard similarity to merge duplicate
trends from different sources into canonical trends. Also checks
against existing trends in Supabase.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Set

from src.db import SupabaseClient, DatabaseError

logger = logging.getLogger(__name__)

# Jaccard similarity threshold for merging trends
SIMILARITY_THRESHOLD = 0.6

# Simple suffix-stripping stemmer rules
_SUFFIX_RULES = [
    ("ying", "y"),
    ("zing", "z"),
    ("ting", "t"),
    ("ning", "n"),
    ("ring", "r"),
    ("ling", "l"),
    ("ding", "d"),
    ("bing", "b"),
    ("ging", "g"),
    ("ping", "p"),
    ("ming", "m"),
    ("king", "k"),
    ("sing", "s"),
    ("ing", ""),
    ("ies", "y"),
    ("ness", ""),
    ("ment", ""),
    ("tion", ""),
    ("sion", ""),
    ("able", ""),
    ("ible", ""),
    ("ful", ""),
    ("less", ""),
    ("ous", ""),
    ("ive", ""),
    ("ed", ""),
    ("er", ""),
    ("est", ""),
    ("ly", ""),
    ("s", ""),
]


def simple_stem(word: str) -> str:
    """
    Apply simple suffix-stripping stemming to a word.

    This is a lightweight alternative to NLTK/spaCy for MVP.
    Only strips common English suffixes to reduce variants.
    """
    if len(word) <= 3:
        return word
    for suffix, replacement in _SUFFIX_RULES:
        if word.endswith(suffix) and len(word) - len(suffix) + len(replacement) >= 3:
            return word[: -len(suffix)] + replacement
    return word


def normalize_topic(topic: str) -> str:
    """
    Normalize a topic string for deduplication matching.

    Steps: lowercase, remove non-alphanumeric (keep spaces), stem each word,
    sort words alphabetically, join with single space.

    Args:
        topic: Raw topic string.

    Returns:
        Normalized topic string.
    """
    if not topic:
        return ""
    # Lowercase and remove non-alphanumeric (keep spaces and hyphens)
    text = re.sub(r"[^\w\s-]", "", topic.lower())
    # Split, stem, filter empty
    words = [simple_stem(w) for w in text.split() if len(w) > 1]
    # Sort for order-independent matching
    words.sort()
    return " ".join(words)


def jaccard_similarity(set_a: Set[str], set_b: Set[str]) -> float:
    """
    Calculate the Jaccard similarity index between two sets.

    J(A, B) = |A intersection B| / |A union B|

    Args:
        set_a: First set of strings.
        set_b: Second set of strings.

    Returns:
        Jaccard similarity coefficient (0.0 to 1.0).
        Returns 0.0 if both sets are empty.
    """
    if not set_a and not set_b:
        return 0.0
    intersection = set_a & set_b
    union = set_a | set_b
    if not union:
        return 0.0
    return len(intersection) / len(union)


def _keyword_set(keywords: List[str]) -> Set[str]:
    """Convert a keyword list to a stemmed set for comparison."""
    return {simple_stem(k.lower()) for k in keywords if k}


def deduplicate_trends(
    trends: List[Dict[str, Any]],
    similarity_threshold: float = SIMILARITY_THRESHOLD,
) -> List[Dict[str, Any]]:
    """
    Deduplicate a list of trend candidates using Jaccard similarity.

    Trends with keyword overlap > threshold are merged into a single
    canonical trend with a combined sources array.

    Args:
        trends: List of trend dicts (each must have 'topic', 'keywords', 'source').
        similarity_threshold: Jaccard threshold above which trends are merged.

    Returns:
        Deduplicated list of trend dicts.
    """
    if not trends:
        return []

    # Track which trends have been merged
    merged_flags = [False] * len(trends)
    canonical: List[Dict[str, Any]] = []

    for i, trend_a in enumerate(trends):
        if merged_flags[i]:
            continue

        # Start a new canonical trend
        merged = _copy_trend(trend_a)
        sources = _ensure_list(merged.get("source", ""))
        keyword_pool = set(merged.get("keywords", []))

        for j in range(i + 1, len(trends)):
            if merged_flags[j]:
                continue

            trend_b = trends[j]
            set_a = _keyword_set(merged.get("keywords", []))
            set_b = _keyword_set(trend_b.get("keywords", []))

            sim = jaccard_similarity(set_a, set_b)

            if sim > similarity_threshold:
                # Merge trend_b into merged
                merged_flags[j] = True
                b_source = trend_b.get("source", "")
                if isinstance(b_source, list):
                    sources.extend(b_source)
                elif b_source:
                    sources.append(b_source)
                keyword_pool.update(trend_b.get("keywords", []))
                # Keep the higher score_hint
                if trend_b.get("score_hint", 0) > merged.get("score_hint", 0):
                    merged["topic"] = trend_b["topic"]
                    merged["score_hint"] = trend_b["score_hint"]
                    merged["source_data"] = trend_b.get("source_data", {})
                logger.debug(
                    "Merged trend '%s' into '%s' (sim=%.2f)",
                    trend_b.get("topic", "?"),
                    merged.get("topic", "?"),
                    sim,
                )

        # Finalize the canonical trend
        merged["sources"] = list(set(sources))
        merged["keywords"] = list(keyword_pool)
        merged["topic_normalized"] = normalize_topic(merged.get("topic", ""))
        canonical.append(merged)

    logger.info(
        "Dedup: %d candidates -> %d canonical trends",
        len(trends), len(canonical),
    )
    return canonical


def check_existing_trends(
    canonical_trends: List[Dict[str, Any]],
    db: SupabaseClient,
) -> List[Dict[str, Any]]:
    """
    Check canonical trends against existing trends in Supabase.

    If a matching trend already exists (by normalized topic), update
    its sources array instead of creating a duplicate.

    Args:
        canonical_trends: Deduplicated trend list.
        db: Supabase client for querying existing trends.

    Returns:
        List of truly new trends (not already in the database).
    """
    new_trends: List[Dict[str, Any]] = []

    for trend in canonical_trends:
        normalized = trend.get("topic_normalized", "")
        if not normalized:
            normalized = normalize_topic(trend.get("topic", ""))
            trend["topic_normalized"] = normalized

        try:
            existing = db.get_trend_by_normalized_topic(normalized)
        except DatabaseError as exc:
            logger.error("Failed to check existing trend: %s", exc)
            new_trends.append(trend)
            continue

        if existing:
            # Update the existing trend's sources array
            existing_sources = existing.get("sources", []) or []
            new_sources = trend.get("sources", [])
            merged_sources = list(set(existing_sources + new_sources))

            if set(merged_sources) != set(existing_sources):
                try:
                    db.update_trend(existing["id"], {"sources": merged_sources})
                    logger.info(
                        "Updated sources for existing trend '%s': %s",
                        existing.get("topic", ""),
                        merged_sources,
                    )
                except DatabaseError as exc:
                    logger.error("Failed to update trend sources: %s", exc)
            else:
                logger.debug(
                    "Trend '%s' already exists with same sources, skipping",
                    normalized,
                )
        else:
            new_trends.append(trend)

    logger.info(
        "Existing trend check: %d candidates -> %d new trends",
        len(canonical_trends), len(new_trends),
    )
    return new_trends


def _copy_trend(trend: Dict[str, Any]) -> Dict[str, Any]:
    """Create a shallow copy of a trend dict."""
    return {
        "topic": trend.get("topic", ""),
        "keywords": list(trend.get("keywords", [])),
        "source": trend.get("source", ""),
        "source_data": trend.get("source_data", {}),
        "score_hint": trend.get("score_hint", 0),
    }


def _ensure_list(value: Any) -> List[str]:
    """Ensure a value is a list of strings."""
    if isinstance(value, list):
        return list(value)
    if isinstance(value, str) and value:
        return [value]
    return []
