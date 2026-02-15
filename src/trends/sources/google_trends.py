"""
Google Trends data source for Sticker Trendz.

Fetches trending search topics via Google Trends' public RSS feed
(https://trends.google.com/trending/rss?geo=US). No authentication
or pytrends required — works from any IP including cloud/CI runners.
"""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional

import requests

from src.trends.sources.reddit import extract_keywords, sanitize_external_text

logger = logging.getLogger(__name__)

_TRENDS_RSS_URL = "https://trends.google.com/trending/rss?geo=US"
_REQUEST_TIMEOUT = 15  # seconds
_RSS_NS = {"ht": "https://trends.google.com/trends/trendingSearches"}


class GoogleTrendsSource:
    """
    Google Trends data source using the public RSS feed.

    Fetches the current US trending search topics. No authentication
    required and works from cloud/CI IP ranges unlike the pytrends API.
    """

    def __init__(
        self,
        session: Optional[requests.Session] = None,
    ) -> None:
        """
        Args:
            session: Pre-built requests.Session (for testing).
        """
        if session is not None:
            self._session = session
        else:
            self._session = requests.Session()
            self._session.headers.update({
                "User-Agent": "sticker-trendz/1.0 (trend monitoring)",
                "Accept": "application/rss+xml, application/xml, text/xml",
            })

    def fetch_trends(self) -> List[Dict[str, Any]]:
        """
        Fetch trending topics from Google Trends RSS feed.

        Returns a list of trend dicts with:
          - topic: Trending search term
          - keywords: Extracted keywords
          - source: 'google_trends'
          - source_data: Raw data including approxTraffic if available

        On error, logs the failure and returns an empty list (graceful degradation).
        """
        try:
            response = self._session.get(_TRENDS_RSS_URL, timeout=_REQUEST_TIMEOUT)
            response.raise_for_status()
        except Exception as exc:
            logger.error(
                "Failed to fetch Google Trends RSS (graceful degradation): %s", exc
            )
            return []

        try:
            root = ET.fromstring(response.content)
        except ET.ParseError as exc:
            logger.error("Failed to parse Google Trends RSS XML: %s", exc)
            return []

        all_trends: List[Dict[str, Any]] = []
        channel = root.find("channel")
        if channel is None:
            logger.warning("Google Trends RSS: no <channel> element found")
            return []

        for item in channel.findall("item"):
            title_el = item.find("title")
            if title_el is None or not title_el.text:
                continue

            term = sanitize_external_text(title_el.text.strip())
            if not term:
                continue

            # approxTraffic is in the ht: namespace, e.g. "200,000+"
            traffic_el = item.find("ht:approx_traffic", _RSS_NS)
            traffic_raw = traffic_el.text if traffic_el is not None else ""
            traffic = int("".join(c for c in (traffic_raw or "") if c.isdigit()) or 0)

            keywords = extract_keywords(term)
            all_trends.append({
                "topic": term,
                "keywords": keywords if keywords else [term.lower()],
                "source": "google_trends",
                "source_data": {
                    "type": "rss_trending",
                    "term": term,
                    "approx_traffic": traffic,
                },
                "score_hint": traffic,
            })

        # Sort by approx traffic descending
        all_trends.sort(key=lambda t: t.get("score_hint", 0), reverse=True)
        logger.info("Google Trends RSS returned %d trend candidates", len(all_trends))
        return all_trends

    def reset_request_count(self) -> None:
        """No-op — RSS feed has no per-cycle request limit tracking needed."""
