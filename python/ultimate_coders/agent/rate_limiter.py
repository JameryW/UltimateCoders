"""Rate limiter for LLM API calls.

Implements Token Bucket (RPM + TPM dual dimension), priority queue,
model fallback chain (Opus -> Sonnet -> Haiku), and Circuit Breaker
for LLM API fault tolerance.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import Enum, IntEnum

logger = logging.getLogger(__name__)


class TaskComplexity(Enum):
    """Task complexity for model selection."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class RequestPriority(IntEnum):
    """Priority levels for LLM requests."""

    LOW = 0       # Background analysis
    MEDIUM = 1    # Context gathering
    HIGH = 2      # Active worker actions
    CRITICAL = 3  # Orchestrator planning


class CircuitState(Enum):
    """Circuit breaker states."""

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass
class TokenBucket:
    """Token bucket for single-dimension rate limiting.

    Tracks capacity and refills tokens at a constant rate.

    Attributes:
        capacity: Maximum number of tokens (burst size).
        refill_rate: Tokens added per second.
        tokens: Current available tokens.
        last_refill: Timestamp of last refill (seconds since epoch).
    """

    capacity: float = 60.0
    refill_rate: float = 1.0  # 60/60 = 1 per second for 60 RPM
    tokens: float = 60.0
    last_refill: float = field(default_factory=time.monotonic)

    def _refill(self) -> None:
        """Refill tokens based on elapsed time."""
        now = time.monotonic()
        elapsed = now - self.last_refill
        refill = elapsed * self.refill_rate
        self.tokens = min(self.tokens + refill, self.capacity)
        self.last_refill = now

    def try_consume(self, amount: float) -> bool:
        """Try to consume tokens. Returns True if successful."""
        self._refill()
        if self.tokens >= amount:
            self.tokens -= amount
            return True
        return False

    def available(self) -> float:
        """Get current available tokens (after refill)."""
        self._refill()
        return self.tokens

    def wait_time(self, amount: float) -> float:
        """Get the time (seconds) until `amount` tokens will be available."""
        self._refill()
        if self.tokens >= amount:
            return 0.0
        if self.refill_rate > 0:
            deficit = amount - self.tokens
            return deficit / self.refill_rate
        return 3600.0  # No refill


@dataclass
class RateLimiterConfig:
    """Configuration for the LLM rate limiter."""

    rpm: float = 60.0
    tpm: float = 100_000.0
    max_concurrent: int = 10


class RateLimiter:
    """Dual-dimension rate limiter for LLM API calls (RPM + TPM).

    Usage:
        limiter = RateLimiter()
        if limiter.try_acquire(estimated_tokens=1000):
            try:
                result = await llm_client.complete(...)
            finally:
                limiter.release()
        else:
            # Handle rate limit
            wait_time = limiter.wait_time(1000)
    """

    def __init__(self, config: RateLimiterConfig | None = None):
        self.config = config or RateLimiterConfig()
        self.rpm_bucket = TokenBucket(
            capacity=self.config.rpm,
            refill_rate=self.config.rpm / 60.0,
            tokens=self.config.rpm,
        )
        self.tpm_bucket = TokenBucket(
            capacity=self.config.tpm,
            refill_rate=self.config.tpm / 60.0,
            tokens=self.config.tpm,
        )
        self.max_concurrent = self.config.max_concurrent
        self._active_count = 0
        self._total_requests = 0
        self._total_tokens = 0

    def try_acquire(self, estimated_tokens: float = 1000.0) -> bool:
        """Try to acquire capacity for a request.

        Args:
            estimated_tokens: Estimated token consumption for this request.

        Returns:
            True if capacity is available, False otherwise.
        """
        if self._active_count >= self.max_concurrent:
            return False

        if not self.rpm_bucket.try_consume(1.0):
            return False

        if not self.tpm_bucket.try_consume(estimated_tokens):
            return False

        self._active_count += 1
        self._total_requests += 1
        self._total_tokens += int(estimated_tokens)
        return True

    def release(self) -> None:
        """Release a request slot after the request completes."""
        self._active_count = max(0, self._active_count - 1)

    def can_request(self, estimated_tokens: float = 1000.0) -> bool:
        """Check if a request can be made without consuming tokens."""
        if self._active_count >= self.max_concurrent:
            return False
        return (
            self.rpm_bucket.available() >= 1.0
            and self.tpm_bucket.available() >= estimated_tokens
        )

    def wait_time(self, estimated_tokens: float = 1000.0) -> float:
        """Get the estimated wait time (seconds) until a request can be made."""
        rpm_wait = self.rpm_bucket.wait_time(1.0)
        tpm_wait = self.tpm_bucket.wait_time(estimated_tokens)
        return max(rpm_wait, tpm_wait)

    @property
    def rpm_available(self) -> float:
        """Current RPM availability."""
        return self.rpm_bucket.available()

    @property
    def tpm_available(self) -> float:
        """Current TPM availability."""
        return self.tpm_bucket.available()

    @property
    def active_count(self) -> int:
        """Number of currently active requests."""
        return self._active_count

    @property
    def total_requests(self) -> int:
        """Total number of requests made."""
        return self._total_requests


@dataclass
class ModelFallbackChain:
    """Model fallback chain for LLM API resilience.

    When the primary model is rate-limited or unavailable,
    requests are automatically routed to the next model in the chain.

    Attributes:
        primary: Best quality, highest cost (e.g., "claude-opus-4-8").
        secondary: Good quality, moderate cost (e.g., "claude-sonnet-4-6").
        tertiary: Acceptable quality, lowest cost (e.g., "claude-haiku-4-5-20251001").
    """

    primary: str = "claude-opus-4-8"
    secondary: str = "claude-sonnet-4-6"
    tertiary: str = "claude-haiku-4-5-20251001"

    def fallback(self, current: str) -> str | None:
        """Get the next model in the fallback chain.

        Args:
            current: The current model name.

        Returns:
            The next model name, or None if already at the last model.
        """
        if current == self.primary:
            return self.secondary
        if current == self.secondary:
            return self.tertiary
        return None

    def select_model(
        self,
        complexity: TaskComplexity,
        primary_available: bool = True,
        secondary_available: bool = True,
    ) -> str:
        """Select a model based on task complexity and availability.

        Args:
            complexity: The task complexity level.
            primary_available: Whether the primary model is available.
            secondary_available: Whether the secondary model is available.

        Returns:
            The selected model name.
        """
        if complexity == TaskComplexity.HIGH:
            return self.primary if primary_available else self.secondary
        if complexity == TaskComplexity.MEDIUM:
            return self.secondary if secondary_available else self.tertiary
        return self.tertiary


class CircuitBreaker:
    """Circuit Breaker for LLM API fault tolerance.

    Implements the Circuit Breaker pattern to prevent cascading failures
    when an LLM API endpoint is degraded.

    States:
        CLOSED: Normal operation. Failures are counted.
        OPEN: All requests rejected immediately. After timeout, transitions to HALF_OPEN.
        HALF_OPEN: Limited requests allowed to test recovery.

    Usage:
        cb = CircuitBreaker()
        if cb.allow_request():
            try:
                result = await llm_client.complete(...)
                cb.record_success()
            except Exception:
                cb.record_failure()
        else:
            # Circuit is open, use fallback
    """

    def __init__(
        self,
        failure_threshold: int = 5,
        success_threshold: int = 2,
        reset_timeout_seconds: float = 30.0,
    ):
        self.failure_threshold = failure_threshold
        self.success_threshold = success_threshold
        self.reset_timeout = reset_timeout_seconds
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._success_count = 0
        self._last_failure_time: float | None = None
        self._total_calls = 0
        self._total_rejected = 0

    def allow_request(self) -> bool:
        """Check if a request is allowed through the circuit.

        Returns:
            True if the request can proceed, False if the circuit is open.
        """
        self._total_calls += 1

        if self._state == CircuitState.CLOSED:
            return True

        if self._state == CircuitState.OPEN:
            # Check if reset timeout has elapsed
            if self._last_failure_time is not None:
                elapsed = time.monotonic() - self._last_failure_time
                if elapsed >= self.reset_timeout:
                    self._state = CircuitState.HALF_OPEN
                    self._success_count = 0
                    return True

            self._total_rejected += 1
            return False

        # HALF_OPEN: allow limited requests
        return True

    def record_success(self) -> None:
        """Record a successful call.

        In half-open state, consecutive successes will close the circuit.
        """
        self._failure_count = 0

        if self._state == CircuitState.HALF_OPEN:
            self._success_count += 1
            if self._success_count >= self.success_threshold:
                self._state = CircuitState.CLOSED
                logger.info("Circuit breaker closed — endpoint recovered")

    def record_failure(self) -> None:
        """Record a failed call.

        In closed state, consecutive failures will open the circuit.
        In half-open state, a single failure will re-open the circuit.
        """
        if self._state == CircuitState.CLOSED:
            self._failure_count += 1
            if self._failure_count >= self.failure_threshold:
                self._state = CircuitState.OPEN
                self._last_failure_time = time.monotonic()
                logger.warning(
                    "Circuit breaker opened after %d failures",
                    self._failure_count,
                )
        elif self._state == CircuitState.HALF_OPEN:
            self._state = CircuitState.OPEN
            self._last_failure_time = time.monotonic()
            self._success_count = 0
            logger.warning("Circuit breaker re-opened from half-open state")
        else:
            # Already open, update timestamp
            self._last_failure_time = time.monotonic()

    @property
    def state(self) -> CircuitState:
        """Get the current circuit state."""
        if self._state == CircuitState.OPEN and self._last_failure_time is not None:
            elapsed = time.monotonic() - self._last_failure_time
            if elapsed >= self.reset_timeout:
                return CircuitState.HALF_OPEN
        return self._state

    @property
    def failure_count(self) -> int:
        """Get the current failure count."""
        return self._failure_count

    @property
    def total_calls(self) -> int:
        """Get the total number of calls attempted."""
        return self._total_calls

    @property
    def total_rejected(self) -> int:
        """Get the total number of rejected calls."""
        return self._total_rejected

    def force_state(self, state: CircuitState) -> None:
        """Force the circuit into a specific state (for testing)."""
        self._state = state

    def reset(self) -> None:
        """Reset the circuit breaker to closed state.

        Clears all failure counts and resets the state to CLOSED.
        Useful for manual recovery via the dashboard when the
        underlying service has recovered but the circuit hasn't
        auto-closed yet.
        """
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._success_count = 0
        self._last_failure_time = None
        logger.info("Circuit breaker manually reset to closed state")


@dataclass
class RetryPolicy:
    """Retry policy with exponential backoff and jitter.

    Attributes:
        max_retries: Maximum number of retry attempts.
        base_delay: Base delay in seconds for exponential backoff.
        max_delay: Maximum delay in seconds between retries.
        jitter: Whether to add jitter to the delay.
    """

    max_retries: int = 5
    base_delay: float = 1.0
    max_delay: float = 60.0
    jitter: bool = True

    def delay_for_attempt(self, attempt: int) -> float:
        """Calculate the delay for a given retry attempt.

        Uses exponential backoff: base_delay * 2^attempt + jitter

        Args:
            attempt: The retry attempt number (0-indexed).

        Returns:
            The delay in seconds.
        """
        import random

        exp_delay = self.base_delay * (2 ** attempt)
        if self.jitter:
            jitter_amount = exp_delay * random.uniform(0, 0.5)  # noqa: S311
        else:
            jitter_amount = 0.0

        return min(exp_delay + jitter_amount, self.max_delay)
