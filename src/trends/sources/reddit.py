"""
Reddit trend source for Sticker Trendz.

Fetches hot posts from configured subreddits via Reddit's public
unauthenticated JSON API (no OAuth credentials required).
Extracts keywords and topics from post titles and content.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Set

import requests

from src.config import load_config
from src.resilience import retry, RetryExhaustedError

logger = logging.getLogger(__name__)

# Default subreddits to monitor
DEFAULT_SUBREDDITS = ["memes", "funny", "trending"]

# Common English stop words to filter out of keyword extraction
MAX_TOPIC_LENGTH = 500
MAX_SELFTEXT_LENGTH = 1000

# Reddit public JSON API — no auth needed
_REDDIT_BASE_URL = "https://www.reddit.com"
_REQUEST_TIMEOUT = 10  # seconds

# Regex for stripping HTML tags and control characters
_HTML_TAG_RE = re.compile(r"<[^>]*>")
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def sanitize_external_text(text: str, max_length: int = MAX_TOPIC_LENGTH) -> str:
    """Strip HTML tags, control characters, and enforce max length on external text."""
    text = _HTML_TAG_RE.sub("", text)
    text = _CONTROL_CHAR_RE.sub("", text)
    return text.strip()[:max_length]


STOP_WORDS: Set[str] = {
    "a", "an", "and", "are", "as", "at", "be", "but", "by", "for",
    "from", "had", "has", "have", "he", "her", "his", "how", "i",
    "if", "in", "into", "is", "it", "its", "just", "me", "my", "no",
    "not", "of", "on", "or", "our", "out", "so", "some", "than",
    "that", "the", "their", "them", "then", "there", "these", "they",
    "this", "to", "too", "up", "us", "very", "was", "we", "were",
    "what", "when", "where", "which", "while", "who", "whom", "why",
    "will", "with", "would", "you", "your", "like", "get", "got",
    "can", "do", "did", "does", "been", "being", "am", "could",
    "should", "shall", "may", "might", "must", "need", "about",
    "after", "before", "between", "during", "each", "few", "more",
    "most", "other", "over", "own", "same", "still", "such", "through",
    "under", "until", "back", "here", "now", "only", "one", "two",
    "new", "old", "good", "bad", "first", "last", "long", "great",
    "little", "never", "also", "around", "another", "because",
    "every", "going", "know", "make", "much", "even", "well", "way",
    "many", "say", "she", "him", "all", "day", "man", "see", "look",
    "come", "think", "tell", "work", "give", "take", "find", "try",
    "let", "put", "keep", "thing", "people", "yeah", "okay", "right",
    "really", "im", "dont", "cant", "ive", "thats",
}


def extract_keywords(text: str, max_keywords: int = 10) -> List[str]:
    """
    Extract meaningful keywords from text.

    Lowercases, removes punctuation, filters stop words, and returns
    unique keywords sorted by length (longer = more specific).

    Args:
        text: Raw text to extract from.
        max_keywords: Maximum keywords to return.

    Returns:
        List of lowercase keyword strings.
    """
    # Remove URLs
    text = re.sub(r"https?://\S+", "", text)
    # Remove special characters but keep spaces and hyphens
    text = re.sub(r"[^\w\s-]", " ", text.lower())
    # Split into words
    words = text.split()
    # Filter stop words and very short words
    keywords = [
        w for w in words
        if w not in STOP_WORDS and len(w) > 2 and not w.isdigit()
    ]
    # Deduplicate while preserving order
    seen: set[str] = set()
    unique: list[str] = []
    for kw in keywords:
        if kw not in seen:
            seen.add(kw)
            unique.append(kw)
    # Sort by length descending (prefer more specific terms)
    unique.sort(key=len, reverse=True)
    return unique[:max_keywords]


class RedditSource:
    """
    Reddit public JSON API client for fetching trending posts.

    Monitors r/memes, r/funny, r/trending, and configurable niche subs.
    Uses unauthenticated requests to <subreddit>/hot.json — no OAuth needed.
    Rate limit: ~10 req/min unauthenticated, well within our 2-hour poll cycle.
    """

    def __init__(
        self,
        user_agent: Optional[str] = None,
        subreddits: Optional[List[str]] = None,
        session: Optional[requests.Session] = None,
    ) -> None:
        """
        Args:
            user_agent: User-Agent header value. Falls back to config/default.
            subreddits: List of subreddit names to monitor.
            session: Pre-built requests.Session (for testing / connection reuse).
        """
        self._subreddits = subreddits or DEFAULT_SUBREDDITS

        cfg = load_config(require_all=False)
        self._user_agent = user_agent or cfg.reddit.user_agent

        if session is not None:
            self._session = session
        else:
            self._session = requests.Session()
            self._session.headers.update({"User-Agent": self._user_agent})

        logger.info("RedditSource initialized (unauthenticated public API)")

    @retry(max_retries=3, service="reddit")
    def _fetch_subreddit_hot(self, subreddit_name: str, limit: int = 25) -> List[Dict[str, Any]]:
        """Fetch hot posts from a single subreddit via the public JSON endpoint."""
        url = f"{_REDDIT_BASE_URL}/r/{subreddit_name}/hot.json"
        response = self._session.get(
            url,
            params={"limit": limit},
            timeout=_REQUEST_TIMEOUT,
        )
        response.raise_for_status()

        data = response.json()
        children = data.get("data", {}).get("children", [])

        posts = []
        for child in children:
            p = child.get("data", {})
            posts.append({
                "id": p.get("id", ""),
                "title": sanitize_external_text(p.get("title", ""), MAX_TOPIC_LENGTH),
                "score": p.get("score", 0),
                "upvote_ratio": p.get("upvote_ratio", 0.0),
                "num_comments": p.get("num_comments", 0),
                "url": p.get("url", ""),
                "selftext": sanitize_external_text(
                    p.get("selftext", ""), MAX_SELFTEXT_LENGTH
                ),
                "subreddit": subreddit_name,
                "created_utc": p.get("created_utc", 0),
            })
        return posts

    def fetch_trends(self, posts_per_sub: int = 25) -> List[Dict[str, Any]]:
        """
        Fetch trending topics from all configured subreddits.

        Returns a list of trend dicts with:
          - topic: Post title (as trend topic)
          - keywords: Extracted keywords from title + body
          - source: 'reddit'
          - source_data: Raw post data
          - score_hint: Upvote score (for prioritization)

        On error, logs the failure and returns an empty list (graceful degradation).
        """
        all_trends: List[Dict[str, Any]] = []

        for sub_name in self._subreddits:
            try:
                posts = self._fetch_subreddit_hot(sub_name, limit=posts_per_sub)
                logger.info(
                    "Fetched %d posts from r/%s", len(posts), sub_name
                )
            except (RetryExhaustedError, Exception) as exc:
                logger.error(
                    "Failed to fetch from r/%s (graceful degradation): %s",
                    sub_name, exc,
                )
                continue

            for post in posts:
                title = post.get("title", "")
                selftext = post.get("selftext", "")
                keywords = extract_keywords(f"{title} {selftext}")

                if not keywords:
                    continue

                all_trends.append({
                    "topic": title,
                    "keywords": keywords,
                    "source": "reddit",
                    "source_data": {
                        "reddit_id": post.get("id"),
                        "subreddit": sub_name,
                        "score": post.get("score", 0),
                        "upvote_ratio": post.get("upvote_ratio", 0),
                        "num_comments": post.get("num_comments", 0),
                    },
                    "score_hint": post.get("score", 0),
                })

        # Sort by Reddit score (most upvoted first)
        all_trends.sort(key=lambda t: t.get("score_hint", 0), reverse=True)
        logger.info("Reddit source returned %d trend candidates", len(all_trends))
        return all_trends
