"""Failure paths of recipient quotas, request limits and provenance."""

import asyncio
from dataclasses import replace
from datetime import date, datetime
from typing import ClassVar, override
from unittest.mock import Mock
from uuid import UUID

import pytest

from persistence_support import NOW, action_write
from processing_support import Gateway, Source, fixture_records
from support import AS_OF, repo_policy
from techcom_followup.adapters.inbound.api.middleware import ApiSettings
from techcom_followup.adapters.inbound.api.rate_limit import (
    LocalRequestLimiter,
)
from techcom_followup.adapters.outbound.admission import (
    PgDeliveryQuota,
    PgRequestLimiter,
)
from techcom_followup.adapters.outbound.clock import FixedClock
from techcom_followup.adapters.outbound.in_memory_repository import (
    InMemoryRepository,
)
from techcom_followup.adapters.outbound.run_lock import InMemoryRunLock
from techcom_followup.application.dispatch import (
    DeliveryDependencies,
    DispatchActions,
)
from techcom_followup.application.process_tasks import (
    ProcessingDependencies,
    ProcessTasks,
)
from techcom_followup.application.repository import (
    ActionRecord,
    ActionStatus,
    DecisionWrite,
    RunRecord,
    StoredDecision,
)
from techcom_followup.infrastructure.resilience import (
    BreakerState,
    CircuitBreaker,
)


class FailingQuota:
    async def allow(self, _action: ActionRecord, _now: datetime) -> bool:
        message = "quota store unavailable"
        raise RuntimeError(message)


@pytest.mark.parametrize("with_breaker", [False, True])
async def test_quota_store_failure_releases_claim_and_probe(
    with_breaker: bool,
) -> None:
    repository = InMemoryRepository()
    await repository.record_decision(await action_write(repository), NOW)
    gateway = Gateway()
    breaker = CircuitBreaker(
        failure_threshold=1, cooldown_s=1, monotonic=lambda: 0.0
    )
    dispatcher = DispatchActions(
        DeliveryDependencies(repository, gateway, FixedClock(NOW)),
        breaker=breaker if with_breaker else None,
        quota=FailingQuota(),
    )
    with pytest.raises(RuntimeError, match="quota store unavailable"):
        await dispatcher.execute(limit=1)
    (action,) = await repository.list_actions()
    assert not gateway.sends
    # An unreserved claim becomes "unknown" and is reconciled, never
    # silently resent; the breaker is not charged for a store failure.
    assert action.status == ActionStatus.UNKNOWN
    assert action.lease_token is None
    assert breaker.state is BreakerState.CLOSED


async def test_denied_quota_returns_breaker_permit() -> None:
    class DeniedQuota:
        async def allow(self, _action: ActionRecord, _now: datetime) -> bool:
            return False

    repository = InMemoryRepository()
    await repository.record_decision(await action_write(repository), NOW)
    breaker = CircuitBreaker(
        failure_threshold=1, cooldown_s=1, monotonic=lambda: 0.0
    )
    dispatcher = DispatchActions(
        DeliveryDependencies(repository, Gateway(), FixedClock(NOW)),
        breaker=breaker,
        quota=DeniedQuota(),
    )
    assert await dispatcher.execute(limit=1) == 0
    assert breaker.state is BreakerState.CLOSED
    assert breaker.admit() is not None


async def test_local_limiter_rejects_unknown_namespace() -> None:
    limiter = LocalRequestLimiter(ApiSettings(), lambda: 0.0)
    with pytest.raises(ValueError, match="unknown admission namespace"):
        await limiter.allow("other", "actor")


def test_shared_limiter_retry_after_covers_the_whole_window() -> None:
    assert PgRequestLimiter(Mock(), period_s=0.2).retry_after == "1"
    assert PgRequestLimiter(Mock(), period_s=60.5).retry_after == "61"


@pytest.mark.parametrize("limit", [0, 10_001, True])
def test_recipient_quota_rejects_unbounded_limits(limit: int) -> None:
    with pytest.raises(ValueError, match=r"in 1\.\.10000"):
        PgDeliveryQuota(Mock(), daily_limit=limit)


async def test_decision_write_rejects_inconsistent_provenance() -> None:
    write = await action_write(InMemoryRepository())
    for changed, message in (
        ({"source_project": ""}, "invalid source project"),
        ({"source_project": "x" * 201}, "invalid source project"),
        ({"source_project": "Other"}, "recipient project differs"),
        ({"source_position": -1}, "invalid source position"),
    ):
        with pytest.raises(ValueError, match=message):
            replace(write, **changed).validate()


async def test_multiple_writer_failures_keep_every_error() -> None:
    class Broken(InMemoryRepository):
        run_ids: ClassVar[list[UUID]] = []

        @override
        async def start_run(
            self, as_of: date, policy_version: str, now: datetime
        ) -> RunRecord:
            run = await super().start_run(as_of, policy_version, now)
            self.run_ids.append(run.id)
            return run

        @override
        async def record_decision(
            self, write: DecisionWrite, now: datetime
        ) -> StoredDecision:
            await asyncio.sleep(0)
            message = "write failed"
            raise RuntimeError(message)

    repository = Broken()
    processor = ProcessTasks(
        ProcessingDependencies(
            repository,
            Source(fixture_records()),
            repo_policy(),
            FixedClock(NOW),
            InMemoryRunLock(),
        ),
    )
    with pytest.raises(ExceptionGroup) as caught:
        await processor.execute(as_of=AS_OF)
    assert len(caught.value.exceptions) > 1
    run = await repository.get_run(Broken.run_ids[0])
    assert run is not None
    assert run.stats is not None
    assert run.stats["status"] == "failed"
