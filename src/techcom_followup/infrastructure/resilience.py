"""Resilience primitives: circuit breaker and daily spending budget.

Both take their clock as a dependency so tests never sleep and never
depend on wall time.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum

from techcom_followup.application.resilience_ports import BreakerPermit


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
        self._probe: BreakerPermit | None = None
        self._generation = 0
        self._identity = object()

    @property
    def state(self) -> BreakerState:
        """Current state, derived from failures and elapsed time."""
        if self._opened_at is None:
            return BreakerState.CLOSED
        if self._monotonic() - self._opened_at >= self._cooldown_s:
            return BreakerState.HALF_OPEN
        return BreakerState.OPEN

    def admit(self) -> BreakerPermit | None:
        """Admit an attempt with exclusive half-open probe ownership."""
        state = self.state
        if state is BreakerState.OPEN or (
            state is BreakerState.HALF_OPEN and self._probe is not None
        ):
            return None
        permit = BreakerPermit(self._identity, self._generation)
        if state is BreakerState.HALF_OPEN:
            self._probe = permit
        return permit

    def allow(self) -> bool:
        """Admit a legacy call; concurrent callers use ``admit``."""
        return self.admit() is not None

    def abandon(self, permit: BreakerPermit) -> None:
        """Release only the probe owned by this cancelled attempt."""
        if self._probe is permit:
            self._probe = None

    def _current(self, permit: BreakerPermit | None) -> bool:
        return permit is None or (
            permit.owner is self._identity
            and permit.generation == self._generation
        )

    def record_success(self, permit: BreakerPermit | None = None) -> None:
        """Close the current generation; ignore stale completions."""
        if not self._current(permit):
            return
        if self._opened_at is not None:
            self._generation += 1
        self._failures = 0
        self._opened_at = None
        self._probe = None

    def record_failure(self, permit: BreakerPermit | None = None) -> None:
        """Count current failures without disturbing newer probes."""
        if not self._current(permit):
            return
        self._failures += 1
        if self._failures >= self._threshold:
            self._opened_at = self._monotonic()
            self._generation += 1
            self._probe = None


@dataclass(frozen=True, slots=True)
class SpendReservation:
    """Admission receipt bound to its issuing budget."""

    amount_usd: Decimal


class DailySpendBudget:
    """In-process daily spending guard for paid API calls.

    Reserve a worst-case attempt cost before awaiting the provider;
    settle exactly once with actual cost or the full bound for unknown
    billing. Outstanding bounds survive UTC midnight. A reported overrun
    is charged fully and latches admission closed until reconfiguration.

    This guard assumes one event loop in one process. Production needs a
    shared durable counter for multiple workers and restarts, and a
    provider-side quota for a hard external spending cap.
    """

    def __init__(
        self, *, limit_usd: Decimal, today: Callable[[], date]
    ) -> None:
        """Create an empty budget for the current day.

        Raises:
            ValueError: If the limit is negative.
        """
        if not limit_usd.is_finite() or limit_usd < 0:
            msg = "limit_usd must not be negative"
            raise ValueError(msg)
        self._limit = limit_usd
        self._today = today
        self._day = today()
        self._spent = Decimal(0)
        self._reservations: dict[int, SpendReservation] = {}
        self._overage = Decimal(0)

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
        return (
            self._overage > 0
            or self.spent_usd + self.reserved_usd >= self._limit
        )

    def add(self, cost_usd: Decimal) -> None:
        """Record spending.

        Raises:
            ValueError: If the cost is negative.
        """
        if not cost_usd.is_finite() or cost_usd < 0:
            msg = "cost_usd must not be negative"
            raise ValueError(msg)
        self._roll_over()
        self._spent += cost_usd

    @property
    def reserved_usd(self) -> Decimal:
        """Outstanding bounds, including calls spanning UTC midnight."""
        return sum(
            (item.amount_usd for item in self._reservations.values()),
            Decimal(0),
        )

    @property
    def overage_usd(self) -> Decimal:
        """Uncovered reported cost retained across midnight."""
        return self._overage

    def reserve(self, maximum_usd: Decimal) -> SpendReservation | None:
        """Atomically admit a bounded attempt before its first await.

        Scope is one event loop in one process. Multi-worker and restart
        safety require a shared durable counter in production. Unsettled
        previous-day attempts reserve against the new day too.

        Raises:
            ValueError: The requested bound is negative or non-finite.
        """
        if not maximum_usd.is_finite() or maximum_usd < 0:
            message = "maximum_usd must be finite and nonnegative"
            raise ValueError(message)
        if (
            self._overage > 0
            or self.spent_usd + self.reserved_usd + maximum_usd > self._limit
        ):
            return None
        receipt = SpendReservation(maximum_usd)
        self._reservations[id(receipt)] = receipt
        return receipt

    def settle(
        self, reservation: SpendReservation, cost_usd: Decimal | None = None
    ) -> None:
        """Settle actual cost or the full bound for unknown bills.

        Cancellation and absent usage cannot prove a zero bill.
        Settlement is synchronous and runs exactly once. Reported
        contract overruns are fully charged, never truncated.

        Raises:
            ValueError: A receipt or cost is invalid.
        """
        cost = reservation.amount_usd if cost_usd is None else cost_usd
        if not cost.is_finite() or cost < 0:
            message = "cost_usd must be finite and nonnegative"
            raise ValueError(message)
        if self._reservations.get(id(reservation)) is not reservation:
            message = "unknown or already settled reservation"
            raise ValueError(message)
        self._reservations.pop(id(reservation))
        self._overage += max(cost - reservation.amount_usd, Decimal(0))
        self.add(cost)
