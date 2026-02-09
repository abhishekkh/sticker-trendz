"""
Reddit trend source for Sticker Trendz.

Fetches rising/trending posts from configured subreddits via the Reddit
OAuth API (using PRAW). Extracts keywords, hashtags, and topics from post
titles and content.
"""

from __future__ import annotations

import logging
import re
import string
from typing import Any, Dict, List, Optional, Set

from src.config import load_config
from src.resilience import retry, RetryExhaustedError

logger = logging.getLogger(__name__)

# Default subreddits to monitor
DEFAULT_SUBREDDITS = ["memes", "funny", "trending"]

# Common English stop words to filter out of keyword extraction
MAX_TOPIC_LENGTH = 500
MAX_SELFTEXT_LENGTH = 1000

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
    Reddit OAuth API client for fetching trending posts.

    Monitors r/memes, r/funny, r/trending, and configurable niche subs.
    Respects 60 req/min rate limit (handled by PRAW internally).
    """

    def __init__(
        self,
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
        user_agent: Optional[str] = None,
        subreddits: Optional[List[str]] = None,
        reddit_client: Optional[Any] = None,
    ) -> None:
        """
        Args:
            client_id: Reddit app client ID. Falls back to config.
            client_secret: Reddit app client secret. Falls back to config.
            user_agent: User agent string. Falls back to config.
            subreddits: List of subreddit names to monitor.
            reddit_client: Pre-built PRAW Reddit instance (for testing).
        """
        self._subreddits = subreddits or DEFAULT_SUBREDDITS
        self._reddit = reddit_client

        if not self._reddit:
            cfg = load_config(require_all=False)
            _id = client_id or cfg.reddit.client_id
            _secret = client_secret or cfg.reddit.client_secret
            _agent = user_agent or cfg.reddit.user_agent

            try:
                import praw
                self._reddit = praw.Reddit(
                    client_id=_id,
                    client_secret=_secret,
                    user_agent=_agent,
                )
                logger.info("Reddit client initialized (read-only)")
            except Exception as exc:
                logger.error("Failed to initialize Reddit client: %s", exc)
                self._reddit = None

    @retry(max_retries=3, service="reddit")
    def _fetch_subreddit_hot(self, subreddit_name: str, limit: int = 25) -> List[Dict[str, Any]]:
        """Fetch hot posts from a single subreddit."""
        if not self._reddit:
            raise RuntimeError("Reddit client not initialized")

        subreddit = self._reddit.subreddit(subreddit_name)
        posts = []
        for submission in subreddit.hot(limit=limit):
            posts.append({
                "id": submission.id,
                "title": sanitize_external_text(submission.title, MAX_TOPIC_LENGTH),
                "score": submission.score,
                "upvote_ratio": submission.upvote_ratio,
                "num_comments": submission.num_comments,
                "url": submission.url,
                "selftext": sanitize_external_text(
                    getattr(submission, "selftext", ""), MAX_SELFTEXT_LENGTH
                ),
                "subreddit": subreddit_name,
                "created_utc": submission.created_utc,
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
