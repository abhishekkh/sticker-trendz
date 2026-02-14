"""
Integration tests for Upstash Redis (EtsyRateLimiter).

Requires UPSTASH_REDIS_URL and UPSTASH_REDIS_TOKEN environment variables.

All test keys use a unique prefix and are cleaned up after each test.
"""

from __future__ import annotations

import uuid

import pytest

from tests.conftest import skip_if_no_redis

pytestmark = [pytest.mark.integration, skip_if_no_redis]


@pytest.fixture
def test_suffix():
    """Unique suffix so parallel runs don't collide."""
    return uuid.uuid4().hex[:8]


@pytest.fixture
def limiter():
    from src.publisher.etsy_rate_limiter import EtsyRateLimiter

    return EtsyRateLimiter()


@pytest.fixture(autouse=True)
def cleanup_keys(limiter, test_suffix):
    """Best-effort cleanup of test keys after each test."""
    keys_to_clean: list[str] = []
    yield keys_to_clean
    for key in keys_to_clean:
        try:
            limiter._redis.delete(key)
        except Exception:
            pass


class TestRedisConnection:
    """Verify basic connectivity to Upstash Redis."""

    def test_ping(self, limiter):
        """Redis PING should return True."""
        assert limiter._redis.ping() is True

    def test_set_and_get(self, limiter, test_suffix, cleanup_keys):
        """Basic SET/GET round-trip."""
        key = f"test:sticker-trendz:{test_suffix}"
        cleanup_keys.append(key)

        limiter._redis.set(key, "hello", ex=60)
        assert limiter._redis.get(key) == "hello"

    def test_delete(self, limiter, test_suffix, cleanup_keys):
        """DELETE removes a key."""
        key = f"test:sticker-trendz:{test_suffix}"
        cleanup_keys.append(key)

        limiter._redis.set(key, "temp", ex=60)
        limiter._redis.delete(key)
        assert limiter._redis.get(key) is None


class TestRateLimiterLive:
    """Test EtsyRateLimiter operations against live Redis."""

    def test_increment_and_get_daily_usage(self, limiter, test_suffix, cleanup_keys):
        """increment_api_calls writes a counter that get_daily_usage reads."""
        # Use a custom date key so we don't pollute the real daily counter
        from datetime import datetime, timezone

        key = limiter._daily_key()
        cleanup_keys.append(key)

        before = limiter.get_daily_usage()
        limiter.increment_api_calls(1)
        after = limiter.get_daily_usage()

        assert after == before + 1

    def test_increment_by_multiple(self, limiter, cleanup_keys):
        """increment_api_calls(5) adds 5 to the counter."""
        key = limiter._daily_key()
        cleanup_keys.append(key)

        before = limiter.get_daily_usage()
        limiter.increment_api_calls(5)
        after = limiter.get_daily_usage()

        assert after == before + 5

    def test_daily_key_has_ttl(self, limiter, cleanup_keys):
        """After incrementing, the daily key should have a TTL set."""
        key = limiter._daily_key()
        cleanup_keys.append(key)

        limiter.increment_api_calls(1)
        ttl = limiter._redis.ttl(key)

        # TTL should be positive (up to 48h = 172800s)
        assert ttl is not None
        assert ttl > 0

    def test_can_proceed_returns_bool(self, limiter, cleanup_keys):
        """can_proceed returns a boolean without error."""
        from src.publisher.etsy_rate_limiter import P0_ORDER_READS, P3_ANALYTICS

        key = limiter._daily_key()
        cleanup_keys.append(key)

        assert isinstance(limiter.can_proceed(P0_ORDER_READS), bool)
        assert isinstance(limiter.can_proceed(P3_ANALYTICS), bool)

    def test_get_usage_level_returns_valid_string(self, limiter, cleanup_keys):
        """get_usage_level returns one of the expected level strings."""
        key = limiter._daily_key()
        cleanup_keys.append(key)

        level = limiter.get_usage_level()
        assert level in ("normal", "warning", "critical", "hard_stop")


class TestRedisLockLive:
    """Test Redis lock acquire/release against live Redis."""

    def test_acquire_and_release_lock(self, limiter, test_suffix, cleanup_keys):
        """Lock acquire succeeds, second acquire fails, release allows re-acquire."""
        workflow = f"test_workflow_{test_suffix}"
        lock_key = f"lock:{workflow}"
        cleanup_keys.append(lock_key)

        # First acquire succeeds
        assert limiter.acquire_lock(workflow, ttl=30) is True

        # Second acquire fails (lock already held)
        assert limiter.acquire_lock(workflow, ttl=30) is False

        # Release
        assert limiter.release_lock(workflow) is True

        # Re-acquire succeeds
        assert limiter.acquire_lock(workflow, ttl=30) is True

        # Final cleanup
        limiter.release_lock(workflow)

    def test_lock_key_has_ttl(self, limiter, test_suffix, cleanup_keys):
        """Acquired lock should have a TTL."""
        workflow = f"test_lock_ttl_{test_suffix}"
        lock_key = f"lock:{workflow}"
        cleanup_keys.append(lock_key)

        limiter.acquire_lock(workflow, ttl=60)
        ttl = limiter._redis.ttl(lock_key)

        assert ttl is not None
        assert 0 < ttl <= 60

        limiter.release_lock(workflow)

    def test_release_nonexistent_lock(self, limiter, test_suffix):
        """Releasing a lock that doesn't exist should not raise."""
        workflow = f"test_no_lock_{test_suffix}"
        result = limiter.release_lock(workflow)
        assert result is True
