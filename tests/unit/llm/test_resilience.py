from datetime import date, timedelta
from decimal import Decimal

import pytest

from llm_doubles import FakeClock
from techcom_followup.infrastructure.resilience import (
    BreakerState,
    CircuitBreaker,
    DailySpendBudget,
)


def state_of(cb: CircuitBreaker) -> BreakerState:
    # A function call stops mypy from narrowing the property between
    # assertions; the state really changes as the clock moves.
    return cb.state


def breaker(clock: FakeClock) -> CircuitBreaker:
    return CircuitBreaker(
        failure_threshold=2,
        cooldown_s=30,
        monotonic=clock.monotonic,
    )


@pytest.mark.parametrize(
    ("threshold", "cooldown"),
    [(0, 30.0), (1, 0.0)],
)
def test_breaker_rejects_invalid_settings(
    threshold: int,
    cooldown: float,
    clock: FakeClock,
) -> None:
    with pytest.raises(ValueError, match="must"):
        CircuitBreaker(
            failure_threshold=threshold,
            cooldown_s=cooldown,
            monotonic=clock.monotonic,
        )


def test_breaker_opens_after_consecutive_failures(clock: FakeClock) -> None:
    cb = breaker(clock)
    cb.record_failure()
    assert state_of(cb) is BreakerState.CLOSED
    assert cb.allow()
    cb.record_failure()
    assert state_of(cb) is BreakerState.OPEN
    assert not cb.allow()


def test_success_resets_the_failure_count(clock: FakeClock) -> None:
    cb = breaker(clock)
    cb.record_failure()
    cb.record_success()
    cb.record_failure()
    assert state_of(cb) is BreakerState.CLOSED


def test_half_open_allows_exactly_one_probe(clock: FakeClock) -> None:
    cb = breaker(clock)
    cb.record_failure()
    cb.record_failure()
    clock.advance(30)
    assert state_of(cb) is BreakerState.HALF_OPEN
    assert cb.allow()
    assert not cb.allow()


def test_failed_probe_reopens_for_another_cooldown(clock: FakeClock) -> None:
    cb = breaker(clock)
    cb.record_failure()
    cb.record_failure()
    clock.advance(30)
    assert cb.allow()
    cb.record_failure()
    assert state_of(cb) is BreakerState.OPEN
    clock.advance(29)
    assert not cb.allow()


def test_successful_probe_closes(clock: FakeClock) -> None:
    cb = breaker(clock)
    cb.record_failure()
    cb.record_failure()
    clock.advance(31)
    assert cb.allow()
    cb.record_success()
    assert state_of(cb) is BreakerState.CLOSED


def test_budget_rolls_over_at_midnight() -> None:
    days = [date(2026, 10, 21)]
    budget = DailySpendBudget(limit_usd=Decimal(1), today=lambda: days[0])
    budget.add(Decimal("0.6"))
    assert not budget.exhausted()
    budget.add(Decimal("0.4"))
    assert budget.exhausted()
    days[0] += timedelta(days=1)
    assert budget.spent_usd == 0
    assert not budget.exhausted()


def test_budget_add_after_rollover_starts_from_zero() -> None:
    days = [date(2026, 10, 21)]
    budget = DailySpendBudget(limit_usd=Decimal(1), today=lambda: days[0])
    budget.add(Decimal("0.9"))
    days[0] += timedelta(days=1)
    budget.add(Decimal("0.1"))
    assert budget.spent_usd == Decimal("0.1")


def test_budget_rejects_negative_values() -> None:
    with pytest.raises(ValueError, match="limit_usd"):
        DailySpendBudget(limit_usd=Decimal(-1), today=date.today)
    budget = DailySpendBudget(limit_usd=Decimal(1), today=date.today)
    with pytest.raises(ValueError, match="cost_usd"):
        budget.add(Decimal("-0.01"))


def test_cancelled_probe_is_released_without_forging_success(
    clock: FakeClock,
) -> None:
    cb = breaker(clock)
    cb.record_failure()
    cb.record_failure()
    clock.advance(30)
    permit = cb.admit()
    assert permit is not None
    cb.abandon(permit)
    assert cb.state is BreakerState.HALF_OPEN
    assert cb.allow()


def test_budget_reserves_and_settles_exactly_once_across_midnight() -> None:
    day = [date(2026, 10, 21)]
    budget = DailySpendBudget(limit_usd=Decimal(1), today=lambda: day[0])
    receipt = budget.reserve(Decimal("0.7"))
    assert receipt is not None
    assert budget.reserve(Decimal("0.4")) is None
    assert budget.reserved_usd == Decimal("0.7")
    day[0] += timedelta(days=1)
    assert budget.spent_usd == 0
    assert budget.reserved_usd == Decimal("0.7")
    budget.settle(receipt, Decimal("0.2"))
    assert budget.spent_usd == Decimal("0.2")
    assert budget.reserved_usd == 0
    with pytest.raises(ValueError, match="settled"):
        budget.settle(receipt)
    unknown = budget.reserve(Decimal("0.8"))
    assert unknown is not None
    assert budget.exhausted()
    budget.settle(unknown)
    assert budget.spent_usd == Decimal(1)


@pytest.mark.parametrize(
    "value", [Decimal(-1), Decimal("NaN"), Decimal("Inf")]
)
def test_budget_rejects_invalid_reservations_and_settlements(
    value: Decimal,
) -> None:
    budget = DailySpendBudget(limit_usd=Decimal(1), today=date.today)
    with pytest.raises(ValueError, match="maximum_usd"):
        budget.reserve(value)
    receipt = budget.reserve(Decimal(1))
    assert receipt is not None
    with pytest.raises(ValueError, match="cost_usd"):
        budget.settle(receipt, value)
    assert budget.reserved_usd == 1


def test_reported_overage_is_charged_and_permanently_blocks_admission() -> (
    None
):
    day = [date(2026, 10, 21)]
    budget = DailySpendBudget(limit_usd=Decimal(10), today=lambda: day[0])
    receipt = budget.reserve(Decimal("0.5"))
    assert receipt is not None
    budget.settle(receipt, Decimal("1.2"))
    assert budget.spent_usd == Decimal("1.2")
    assert budget.overage_usd == Decimal("0.7")
    assert budget.exhausted()
    assert budget.reserve(Decimal("0.1")) is None
    day[0] += timedelta(days=1)
    assert budget.spent_usd == 0
    assert budget.overage_usd == Decimal("0.7")
    assert budget.reserve(Decimal(0)) is None


def test_stale_completion_cannot_release_or_change_new_probe(
    clock: FakeClock,
) -> None:
    cb = breaker(clock)
    stale = cb.admit()
    assert stale is not None
    cb.record_failure()
    cb.record_failure()
    clock.advance(30)
    probe = cb.admit()
    assert probe is not None
    cb.abandon(stale)
    cb.record_success(stale)
    cb.record_failure(stale)
    assert cb.admit() is None
    cb.record_success(probe)
    assert cb.state is BreakerState.CLOSED


def test_foreign_permit_cannot_complete_breaker(clock: FakeClock) -> None:
    cb = breaker(clock)
    foreign = breaker(clock).admit()
    assert foreign is not None
    cb.record_failure()
    cb.record_success(foreign)
    cb.record_failure(foreign)
    cb.record_failure()
    assert cb.state is BreakerState.OPEN


def test_closed_success_does_not_hide_later_concurrent_failure(
    clock: FakeClock,
) -> None:
    cb = CircuitBreaker(
        failure_threshold=1, cooldown_s=30, monotonic=clock.monotonic
    )
    healthy = cb.admit()
    failing = cb.admit()
    assert healthy is not None
    assert failing is not None
    cb.record_success(healthy)
    cb.record_failure(failing)
    assert cb.state is BreakerState.OPEN
