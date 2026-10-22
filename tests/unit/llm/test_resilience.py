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


@pytest.mark.asvs("16.5.2")
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
