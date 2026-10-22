"""Resilience primitives: circuit breaker and daily spending budget.

Both take their clock as a dependency so tests never sleep and never
depend on wall time.
"""

from collections.abc import Callable
from datetime import date
from decimal import Decimal
from enum import StrEnum


class BreakerState(StrEnum):
    """Circuit breaker states."""

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    """Consecutive-failure circuit breaker with one half-open probe.

    The breaker opens after ``failure_threshold`` consecutive failures.
    After ``cooldown_s`` it lets exactly one probe through; a success
    closes it, a failure re-opens it for another cooldown.
    """

    def __init__(
        self,
        *,
        failure_threshold: int,
        cooldown_s: float,
        monotonic: Callable[[], float],
    ) -> None:
        """Create a closed breaker.

        Raises:
            ValueError: If the threshold or cooldown is not positive.
        """
        if failure_threshold < 1:
            msg = "failure_threshold must be at least 1"
            raise ValueError(msg)
        if cooldown_s <= 0:
            msg = "cooldown_s must be positive"
            raise ValueError(msg)
        self._threshold = failure_threshold
        self._cooldown_s = cooldown_s
        self._monotonic = monotonic
        self._failures = 0
        self._opened_at: float | None = None
        self._probe_in_flight = False

    @property
    def state(self) -> BreakerState:
        """Current state, derived from failures and elapsed time."""
        if self._opened_at is None:
            return BreakerState.CLOSED
        if self._monotonic() - self._opened_at >= self._cooldown_s:
            return BreakerState.HALF_OPEN
        return BreakerState.OPEN

    def allow(self) -> bool:
        """Return whether a call may proceed right now."""
        state = self.state
        if state is BreakerState.CLOSED:
            return True
        if state is BreakerState.OPEN or self._probe_in_flight:
            return False
        self._probe_in_flight = True
        return True

    def record_success(self) -> None:
        """Close the breaker and reset the failure count."""
        self._failures = 0
        self._opened_at = None
        self._probe_in_flight = False

    def record_failure(self) -> None:
        """Count a failure; open the breaker at the threshold."""
        self._probe_in_flight = False
        self._failures += 1
        if self._failures >= self._threshold:
            self._opened_at = self._monotonic()


class DailySpendBudget:
    """In-process daily spending guard for paid API calls.

    The counter resets when the (UTC) calendar day changes. Production
    persists the counter so that several workers share one budget; this
    class is the per-process guard and the reference behaviour.
    """

    def __init__(
        self, *, limit_usd: Decimal, today: Callable[[], date]
    ) -> None:
        """Create an empty budget for the current day.

        Raises:
            ValueError: If the limit is negative.
        """
        if limit_usd < 0:
            msg = "limit_usd must not be negative"
            raise ValueError(msg)
        self._limit = limit_usd
        self._today = today
        self._day = today()
        self._spent = Decimal(0)

    def _roll_over(self) -> None:
        current = self._today()
        if current != self._day:
            self._day = current
            self._spent = Decimal(0)

    @property
    def spent_usd(self) -> Decimal:
        """Amount spent today."""
        self._roll_over()
        return self._spent

    def exhausted(self) -> bool:
        """Return whether today's limit has been reached."""
        return self.spent_usd >= self._limit

    def add(self, cost_usd: Decimal) -> None:
        """Record spending.

        Raises:
            ValueError: If the cost is negative.
        """
        if cost_usd < 0:
            msg = "cost_usd must not be negative"
            raise ValueError(msg)
        self._roll_over()
        self._spent += cost_usd
