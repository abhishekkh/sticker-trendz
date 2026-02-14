"""Tests for src/publisher/etsy_rate_limiter.py -- rate limit thresholds and Redis locks."""

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from src.publisher.etsy_rate_limiter import (
    EtsyRateLimiter,
    RateLimiterError,
    P0_ORDER_READS,
    P1_NEW_LISTINGS,
    P2_PRICE_UPDATES,
    P3_ANALYTICS,
)


def _make_mock_redis(usage: int = 0) -> MagicMock:
    """Create a mock Redis client that returns the given usage for get()."""
    redis = MagicMock()
    redis.get.return_value = str(usage) if usage else None
    redis.incrby.return_value = usage + 1
    redis.set.return_value = True
    redis.delete.return_value = 1
    redis.ttl.return_value = -1
    return redis


class TestThresholdLogic:
    """Test EtsyRateLimiter._check_threshold (pure logic, no Redis)."""

    def test_0_to_7000_allows_all_priorities(self):
        """0-7000 calls: all priorities P0-P3 allowed."""
        for usage in [0, 1000, 5000, 7000]:
            assert EtsyRateLimiter._check_threshold(usage, P0_ORDER_READS) is True
            assert EtsyRateLimiter._check_threshold(usage, P1_NEW_LISTINGS) is True
            assert EtsyRateLimiter._check_threshold(usage, P2_PRICE_UPDATES) is True
            assert EtsyRateLimiter._check_threshold(usage, P3_ANALYTICS) is True

    def test_7001_to_8500_blocks_p3_only(self):
        """7001-8500: P0, P1, P2 allowed; P3 blocked."""
        for usage in [7001, 8000, 8500]:
            assert EtsyRateLimiter._check_threshold(usage, P0_ORDER_READS) is True
            assert EtsyRateLimiter._check_threshold(usage, P1_NEW_LISTINGS) is True
            assert EtsyRateLimiter._check_threshold(usage, P2_PRICE_UPDATES) is True
            assert EtsyRateLimiter._check_threshold(usage, P3_ANALYTICS) is False

    def test_8501_to_9500_blocks_p2_and_p3(self):
        """8501-9500: only P0 and P1 allowed; P2 and P3 blocked."""
        for usage in [8501, 9000, 9500]:
            assert EtsyRateLimiter._check_threshold(usage, P0_ORDER_READS) is True
            assert EtsyRateLimiter._check_threshold(usage, P1_NEW_LISTINGS) is True
            assert EtsyRateLimiter._check_threshold(usage, P2_PRICE_UPDATES) is False
            assert EtsyRateLimiter._check_threshold(usage, P3_ANALYTICS) is False

    def test_9501_plus_blocks_all(self):
        """9501+: all operations blocked."""
        for usage in [9501, 10000, 100000]:
            assert EtsyRateLimiter._check_threshold(usage, P0_ORDER_READS) is False
            assert EtsyRateLimiter._check_threshold(usage, P1_NEW_LISTINGS) is False
            assert EtsyRateLimiter._check_threshold(usage, P2_PRICE_UPDATES) is False
            assert EtsyRateLimiter._check_threshold(usage, P3_ANALYTICS) is False


class TestCanProceed:
    """Test can_proceed() with mocked Redis."""

    def test_can_proceed_uses_daily_usage(self):
        """can_proceed() uses get_daily_usage() and applies thresholds."""
        redis = _make_mock_redis(usage=5000)
        limiter = EtsyRateLimiter(redis_client=redis)
        assert limiter.can_proceed(P3_ANALYTICS) is True

    def test_can_proceed_blocks_p3_when_over_7000(self):
        """When usage > 7000, P3 is blocked."""
        redis = _make_mock_redis(usage=7200)
        limiter = EtsyRateLimiter(redis_client=redis)
        assert limiter.can_proceed(P0_ORDER_READS) is True
        assert limiter.can_proceed(P3_ANALYTICS) is False

    def test_can_proceed_blocks_all_when_over_9500(self):
        """When usage > 9500, all priorities blocked."""
        redis = _make_mock_redis(usage=9600)
        limiter = EtsyRateLimiter(redis_client=redis)
        assert limiter.can_proceed(P0_ORDER_READS) is False
        assert limiter.can_proceed(P1_NEW_LISTINGS) is False


class TestIncrementAndUsage:
    """Test increment_api_calls and get_daily_usage."""

    def test_increment_api_calls_returns_new_total(self):
        """increment_api_calls increments and returns new total."""
        redis = MagicMock()
        redis.get.return_value = "100"
        redis.incrby.return_value = 101
        redis.ttl.return_value = -1
        limiter = EtsyRateLimiter(redis_client=redis)
        result = limiter.increment_api_calls(1)
        assert result == 101
        redis.incrby.assert_called_once()

    def test_get_daily_usage_returns_stored_value(self):
        """get_daily_usage returns the stored daily count."""
        redis = _make_mock_redis(usage=500)
        limiter = EtsyRateLimiter(redis_client=redis)
        assert limiter.get_daily_usage() == 500

    def test_get_daily_usage_returns_zero_when_no_key(self):
        """get_daily_usage returns 0 when key does not exist."""
        redis = _make_mock_redis(usage=0)
        redis.get.return_value = None
        limiter = EtsyRateLimiter(redis_client=redis)
        assert limiter.get_daily_usage() == 0


class TestRedisLock:
    """Test Redis lock acquire and release."""

    def test_acquire_lock_succeeds_when_not_held(self):
        """acquire_lock returns True when lock is not held."""
        redis = MagicMock()
        redis.set.return_value = True  # nx=True, key didn't exist
        redis.get.return_value = None
        limiter = EtsyRateLimiter(redis_client=redis)
        result = limiter.acquire_lock("trend_monitor")
        assert result is True
        redis.set.assert_called_once()
        call_kwargs = redis.set.call_args[1]
        assert call_kwargs.get("nx") is True
        assert call_kwargs.get("ex") == 25 * 60  # trend_monitor TTL

    def test_acquire_lock_fails_when_already_held(self):
        """acquire_lock returns False when lock is already held."""
        redis = MagicMock()
        redis.set.return_value = False  # nx=True, key exists
        limiter = EtsyRateLimiter(redis_client=redis)
        result = limiter.acquire_lock("trend_monitor")
        assert result is False

    def test_release_lock_deletes_key(self):
        """release_lock uses Lua check-and-delete and returns True when this instance owns the lock."""
        redis = MagicMock()
        redis.set.return_value = True   # acquire succeeds
        redis.eval.return_value = 1     # Lua script: key deleted
        limiter = EtsyRateLimiter(redis_client=redis)
        # Acquire first so _lock_tokens is populated
        limiter.acquire_lock("trend_monitor")
        result = limiter.release_lock("trend_monitor")
        assert result is True
        redis.eval.assert_called_once()
        call_args = redis.eval.call_args[0]
        assert call_args[2] == "lock:trend_monitor"  # KEYS[1]

    def test_release_lock_returns_false_when_not_owned(self):
        """release_lock returns False when we never acquired the lock."""
        redis = MagicMock()
        limiter = EtsyRateLimiter(redis_client=redis)
        # Do NOT acquire; no token stored
        result = limiter.release_lock("pricing_engine")
        assert result is False
        redis.eval.assert_not_called()

    def test_release_lock_returns_false_when_lock_stolen(self):
        """release_lock returns False when the Lua script finds a different owner."""
        redis = MagicMock()
        redis.set.return_value = True   # acquire succeeds
        redis.eval.return_value = 0     # Lua: token mismatch, not deleted
        limiter = EtsyRateLimiter(redis_client=redis)
        limiter.acquire_lock("trend_monitor")
        result = limiter.release_lock("trend_monitor")
        assert result is False

    def test_release_lock_handles_redis_error(self):
        """release_lock returns False on Redis error during eval."""
        redis = MagicMock()
        redis.set.return_value = True
        redis.eval.side_effect = Exception("Connection refused")
        limiter = EtsyRateLimiter(redis_client=redis)
        limiter.acquire_lock("pricing_engine")
        result = limiter.release_lock("pricing_engine")
        assert result is False


class TestUsageLevel:
    """Test get_usage_level string."""

    def test_usage_level_normal(self):
        """Usage 0-7000 returns 'normal'."""
        redis = _make_mock_redis(usage=5000)
        limiter = EtsyRateLimiter(redis_client=redis)
        assert limiter.get_usage_level() == "normal"

    def test_usage_level_warning(self):
        """Usage 7001-8500 returns 'warning'."""
        redis = _make_mock_redis(usage=8000)
        limiter = EtsyRateLimiter(redis_client=redis)
        assert limiter.get_usage_level() == "warning"

    def test_usage_level_critical(self):
        """Usage 8501-9500 returns 'critical'."""
        redis = _make_mock_redis(usage=9000)
        limiter = EtsyRateLimiter(redis_client=redis)
        assert limiter.get_usage_level() == "critical"

    def test_usage_level_hard_stop(self):
        """Usage 9501+ returns 'hard_stop'."""
        redis = _make_mock_redis(usage=9600)
        limiter = EtsyRateLimiter(redis_client=redis)
        assert limiter.get_usage_level() == "hard_stop"
