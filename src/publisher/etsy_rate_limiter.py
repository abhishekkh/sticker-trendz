"""
Upstash Redis client and Etsy API rate limiter for Sticker Trendz.

Tracks daily Etsy API call counts with priority-based throttling.
Also provides Redis-based concurrency locks for workflow deduplication.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from src.config import load_config

logger = logging.getLogger(__name__)

# Priority levels for Etsy API operations
P0_ORDER_READS = 0
P1_NEW_LISTINGS = 1
P2_PRICE_UPDATES = 2
P3_ANALYTICS = 3

# Threshold boundaries (daily call counts)
THRESHOLD_NORMAL = 7_000
THRESHOLD_WARNING = 8_500
THRESHOLD_CRITICAL = 9_500

# Lock TTLs (seconds)
LOCK_TTLS = {
    "trend_monitor": 25 * 60,      # 25 minutes
    "pricing_engine": 30 * 60,     # 30 minutes
    "analytics_sync": 30 * 60,     # 30 minutes
}


class RateLimiterError(Exception):
    """Raised on Redis operation failures."""


class EtsyRateLimiter:
    """
    Redis-backed rate limiter for the Etsy API.

    Tracks daily API call counts using key format etsy_api_calls:{YYYY-MM-DD}
    with 48-hour TTL. Implements priority-based throttling:

      - Normal  (0-7000):   All operations proceed.
      - Warning (7001-8500): Skip P3 (analytics).
      - Critical(8501-9500): Only P0 (orders) and P1 (listings).
      - Hard stop (9501+):  Halt everything.

    Also provides Redis-based concurrency locks for workflow deduplication.
    """

    def __init__(
        self,
        redis_url: Optional[str] = None,
        redis_token: Optional[str] = None,
        redis_client: Optional[object] = None,
    ) -> None:
        """
        Args:
            redis_url: Upstash Redis URL. Falls back to config.
            redis_token: Upstash Redis token. Falls back to config.
            redis_client: Pre-built Redis client (for testing).
        """
        if redis_client is not None:
            self._redis = redis_client
            return

        cfg = load_config(require_all=False)
        url = redis_url or cfg.redis.url
        token = redis_token or cfg.redis.token

        try:
            import redis as redis_lib

            self._redis = redis_lib.Redis.from_url(
                url,
                password=token,
                decode_responses=True,
                socket_timeout=5,
                socket_connect_timeout=5,
            )
            logger.info("Redis client initialized for Etsy rate limiter")
        except Exception as exc:
            raise RateLimiterError(f"Failed to connect to Redis: {exc}") from exc

    def _daily_key(self, date: Optional[datetime] = None) -> str:
        """Return the Redis key for a given date's API call counter."""
        d = date or datetime.now(timezone.utc)
        return f"etsy_api_calls:{d.strftime('%Y-%m-%d')}"

    def increment_api_calls(self, count: int = 1) -> int:
        """
        Atomically increment the daily Etsy API call counter.

        Args:
            count: Number of calls to add (default 1).

        Returns:
            The new total for today.
        """
        key = self._daily_key()
        try:
            new_value = self._redis.incrby(key, count)
            # Set 48-hour TTL if it's a new key
            ttl = self._redis.ttl(key)
            if ttl is None or ttl < 0:
                self._redis.expire(key, 48 * 3600)
            return int(new_value)
        except Exception as exc:
            logger.error("Failed to increment API call counter: %s", exc)
            raise RateLimiterError(f"Redis increment failed: {exc}") from exc

    def get_daily_usage(self, date: Optional[datetime] = None) -> int:
        """
        Return the current day's Etsy API call count.

        Args:
            date: Optional date to query. Defaults to today (UTC).

        Returns:
            Number of API calls made today.
        """
        key = self._daily_key(date)
        try:
            val = self._redis.get(key)
            return int(val) if val is not None else 0
        except Exception as exc:
            logger.error("Failed to get daily API usage: %s", exc)
            return 0

    def can_proceed(self, priority: int) -> bool:
        """
        Check whether an operation at the given priority level should proceed.

        Args:
            priority: Operation priority (P0=0 through P3=3).

        Returns:
            True if the operation is allowed under current usage levels.
        """
        usage = self.get_daily_usage()
        return self._check_threshold(usage, priority)

    @staticmethod
    def _check_threshold(usage: int, priority: int) -> bool:
        """
        Pure logic for threshold checking (testable without Redis).

        Args:
            usage: Current daily API call count.
            priority: Operation priority (0-3).

        Returns:
            True if allowed, False if blocked.
        """
        if usage > THRESHOLD_CRITICAL:
            # Hard stop: block everything
            logger.warning(
                "Etsy API HARD STOP: %d calls used, blocking priority %d",
                usage, priority,
            )
            return False
        if usage > THRESHOLD_WARNING:
            # Critical zone: only P0 and P1
            allowed = priority <= P1_NEW_LISTINGS
            if not allowed:
                logger.warning(
                    "Etsy API CRITICAL: %d calls used, blocking priority %d",
                    usage, priority,
                )
            return allowed
        if usage > THRESHOLD_NORMAL:
            # Warning zone: skip P3
            allowed = priority <= P2_PRICE_UPDATES
            if not allowed:
                logger.info(
                    "Etsy API WARNING: %d calls used, skipping priority %d",
                    usage, priority,
                )
            return allowed
        # Normal zone: all operations proceed
        return True

    def get_usage_level(self) -> str:
        """
        Return a human-readable string for the current usage level.

        Returns:
            One of 'normal', 'warning', 'critical', 'hard_stop'.
        """
        usage = self.get_daily_usage()
        if usage > THRESHOLD_CRITICAL:
            return "hard_stop"
        if usage > THRESHOLD_WARNING:
            return "critical"
        if usage > THRESHOLD_NORMAL:
            return "warning"
        return "normal"

    # ------------------------------------------------------------------
    # Concurrency locks
    # ------------------------------------------------------------------

    def acquire_lock(self, workflow_name: str, ttl: Optional[int] = None) -> bool:
        """
        Attempt to acquire a Redis-based concurrency lock.

        Args:
            workflow_name: The workflow to lock (e.g. 'trend_monitor').
            ttl: Lock TTL in seconds. Defaults to LOCK_TTLS or 30 min.

        Returns:
            True if the lock was acquired, False if already held.
        """
        lock_key = f"lock:{workflow_name}"
        lock_ttl = ttl or LOCK_TTLS.get(workflow_name, 30 * 60)

        try:
            acquired = self._redis.set(lock_key, "1", nx=True, ex=lock_ttl)
            if acquired:
                logger.info(
                    "Acquired lock for '%s' (TTL=%ds)", workflow_name, lock_ttl
                )
                return True
            else:
                logger.info("Lock already held for '%s'", workflow_name)
                return False
        except Exception as exc:
            logger.error("Failed to acquire lock for '%s': %s", workflow_name, exc)
            return False

    def release_lock(self, workflow_name: str) -> bool:
        """
        Release a concurrency lock.

        Args:
            workflow_name: The workflow whose lock to release.

        Returns:
            True if the lock was released, False on error.
        """
        lock_key = f"lock:{workflow_name}"
        try:
            self._redis.delete(lock_key)
            logger.info("Released lock for '%s'", workflow_name)
            return True
        except Exception as exc:
            logger.error("Failed to release lock for '%s': %s", workflow_name, exc)
            return False
