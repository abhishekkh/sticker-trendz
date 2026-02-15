"""
Google Trends data source for Sticker Trendz.

Fetches breakout search terms using the pytrends library. Rate-limited
to 5 requests per cycle to avoid IP blocks.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

from src.trends.sources.reddit import extract_keywords, sanitize_external_text

logger = logging.getLogger(__name__)

MAX_REQUESTS_PER_CYCLE = 5


class GoogleTrendsSource:
    """
    Google Trends data source using the pytrends library.

    Fetches trending/breakout search terms with their relative interest
    scores. No authentication required.
    """

    def __init__(
        self,
        pytrends_client: Optional[Any] = None,
        max_requests: int = MAX_REQUESTS_PER_CYCLE,
    ) -> None:
        """
        Args:
            pytrends_client: Pre-built pytrends TrendReq instance (for testing).
            max_requests: Max API requests per cycle (default 5).
        """
        self._max_requests = max_requests
        self._pytrends = pytrends_client
        self._request_count = 0

        if not self._pytrends:
            try:
                from pytrends.request import TrendReq
                self._pytrends = TrendReq(hl="en-US", tz=360)
                logger.info("Google Trends client initialized")
            except Exception as exc:
                logger.error("Failed to initialize pytrends: %s", exc)
                self._pytrends = None

    def _can_request(self) -> bool:
        """Check if we have remaining requests in this cycle."""
        return self._request_count < self._max_requests

    def fetch_trends(self) -> List[Dict[str, Any]]:
        """
        Fetch breakout trending search terms from Google Trends.

        Returns a list of trend dicts with:
          - topic: Trending search term
          - keywords: Extracted keywords
          - source: 'google_trends'
          - source_data: Raw data including relative interest score

        On error, logs the failure and returns an empty list (graceful degradation).
        """
        if not self._pytrends:
            logger.warning("Google Trends client not available")
            return []

        all_trends: List[Dict[str, Any]] = []

        # Fetch today's trending searches via the dailytrends endpoint
        try:
            if not self._can_request():
                logger.info("Google Trends request limit reached for this cycle")
                return all_trends

            today = self._pytrends.today_searches(pn="US")
            self._request_count += 1

            if today is not None and not today.empty:
                for term in today:
                    term = sanitize_external_text(str(term).strip())
                    if not term:
                        continue
                    keywords = extract_keywords(term)
                    all_trends.append({
                        "topic": term,
                        "keywords": keywords if keywords else [term.lower()],
                        "source": "google_trends",
                        "source_data": {
                            "type": "today_search",
                            "term": term,
                        },
                        "score_hint": 0,
                    })
                logger.info(
                    "Fetched %d today's searches from Google Trends",
                    len(all_trends),
                )
        except Exception as exc:
            logger.error(
                "Failed to fetch today's searches (graceful degradation): %s",
                exc,
            )

        logger.info("Google Trends source returned %d trend candidates", len(all_trends))
        return all_trends

    def reset_request_count(self) -> None:
        """Reset the per-cycle request counter."""
        self._request_count = 0
