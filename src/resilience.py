"""
Resilience utilities for Sticker Trendz.

Provides a retry decorator with exponential backoff and a per-service
circuit breaker. Every external API call should use these wrappers.
"""

from __future__ import annotations

import functools
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional, Type, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Circuit Breaker
# ---------------------------------------------------------------------------

# Default thresholds per service (from spec)
DEFAULT_THRESHOLDS: Dict[str, int] = {
    "reddit": 5,
    "google_trends": 5,
    "openai": 5,
    "replicate": 3,
    "etsy": 3,
    "supabase": 3,
    "r2": 3,
}


@dataclass
class CircuitBreakerState:
    """Tracks consecutive failures for a single service."""

    service: str
    threshold: int
    consecutive_failures: int = 0
    is_open: bool = False

    def record_failure(self) -> bool:
        """Record a failure. Returns True if the circuit just tripped open."""
        self.consecutive_failures += 1
        if self.consecutive_failures >= self.threshold and not self.is_open:
            self.is_open = True
            logger.warning(
                "Circuit breaker OPEN for service '%s' after %d consecutive failures",
                self.service,
                self.consecutive_failures,
            )
            return True
        return False

    def record_success(self) -> None:
        """Record a success, resetting the failure counter."""
        if self.consecutive_failures > 0:
            logger.debug(
                "Circuit breaker reset for service '%s' after success",
                self.service,
            )
        self.consecutive_failures = 0
        self.is_open = False

    def can_proceed(self) -> bool:
        """Return True if the circuit is closed (calls allowed)."""
        return not self.is_open


class CircuitBreakerRegistry:
    """
    Registry of circuit breakers, one per service name.

    State resets between workflow runs (not persisted) -- instantiate
    a fresh registry at the start of each workflow.
    """

    def __init__(
        self, thresholds: Optional[Dict[str, int]] = None
    ) -> None:
        self._thresholds = thresholds or DEFAULT_THRESHOLDS
        self._states: Dict[str, CircuitBreakerState] = {}

    def get(self, service: str) -> CircuitBreakerState:
        if service not in self._states:
            threshold = self._thresholds.get(service, 5)
            self._states[service] = CircuitBreakerState(
                service=service, threshold=threshold
            )
        return self._states[service]

    def reset_all(self) -> None:
        """Reset all circuit breakers (e.g. at the start of a new run)."""
        self._states.clear()


# Module-level default registry -- replace per workflow run if needed.
circuit_breakers = CircuitBreakerRegistry()


# ---------------------------------------------------------------------------
# Retry Decorator
# ---------------------------------------------------------------------------


class RetryExhaustedError(Exception):
    """Raised when all retry attempts are exhausted."""

    def __init__(self, last_exception: Exception, attempts: int):
        self.last_exception = last_exception
        self.attempts = attempts
        super().__init__(
            f"All {attempts} retry attempts exhausted. "
            f"Last error: {last_exception}"
        )


def retry(
    max_retries: int = 3,
    backoff_base: float = 2.0,
    backoff_max: float = 30.0,
    retryable_exceptions: Tuple[Type[Exception], ...] = (Exception,),
    service: Optional[str] = None,
    cb_registry: Optional[CircuitBreakerRegistry] = None,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> Callable:
    """
    Decorator: retry with exponential backoff and optional circuit breaker.

    Args:
        max_retries: Maximum number of retry attempts (default 3).
        backoff_base: Base for exponential wait (default 2 -> 2s, 4s, 8s ...).
        backoff_max: Maximum wait time in seconds (default 30).
        retryable_exceptions: Tuple of exception types that trigger a retry.
        service: If provided, integrates with the circuit breaker for this service.
        cb_registry: Circuit breaker registry to use (defaults to module-level).
        sleep_fn: Sleep function (injectable for testing).
    """

    def decorator(fn: Callable) -> Callable:
        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            registry = cb_registry or circuit_breakers

            # Check circuit breaker before attempting
            if service:
                cb = registry.get(service)
                if not cb.can_proceed():
                    logger.warning(
                        "Circuit breaker open for '%s' -- skipping call to %s",
                        service,
                        fn.__name__,
                    )
                    raise RetryExhaustedError(
                        RuntimeError(
                            f"Circuit breaker open for service '{service}'"
                        ),
                        attempts=0,
                    )

            last_exception: Optional[Exception] = None
            for attempt in range(1, max_retries + 1):
                try:
                    result = fn(*args, **kwargs)
                    # Success -- record it on the circuit breaker
                    if service:
                        registry.get(service).record_success()
                    return result
                except retryable_exceptions as exc:
                    last_exception = exc
                    if service:
                        registry.get(service).record_failure()
                        if not registry.get(service).can_proceed():
                            logger.error(
                                "Circuit breaker tripped for '%s' on attempt %d/%d: %s",
                                service,
                                attempt,
                                max_retries,
                                exc,
                            )
                            break

                    if attempt < max_retries:
                        wait = min(
                            backoff_base ** attempt, backoff_max
                        )
                        logger.warning(
                            "Retry %d/%d for %s (service=%s) in %.1fs: %s",
                            attempt,
                            max_retries,
                            fn.__name__,
                            service or "unknown",
                            wait,
                            exc,
                        )
                        sleep_fn(wait)
                    else:
                        logger.error(
                            "All %d retries exhausted for %s (service=%s): %s",
                            max_retries,
                            fn.__name__,
                            service or "unknown",
                            exc,
                        )

            assert last_exception is not None
            raise RetryExhaustedError(last_exception, max_retries)

        return wrapper

    return decorator
