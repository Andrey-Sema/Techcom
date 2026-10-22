import asyncio
from dataclasses import replace
from datetime import date, timedelta
from typing import override
from uuid import UUID

import pytest

from persistence_support import NOW, action_write
from processing_support import Gateway, Source, fixture_records
from support import AS_OF, repo_policy
from techcom_followup.adapters.outbound.clock import FixedClock
from techcom_followup.adapters.outbound.in_memory_repository import (
    InMemoryRepository,
)
from techcom_followup.adapters.outbound.run_lock import InMemoryRunLock
from techcom_followup.application.dispatch import (
    DeliveryDependencies,
    DispatchActions,
    delivery_status,
)
from techcom_followup.application.lifecycle import graceful_shutdown
from techcom_followup.application.process_tasks import (
    ProcessingDependencies,
    ProcessTasks,
)
from techcom_followup.application.processing_ports import (
    DeliveryKind,
    DeliveryResult,
    MessageDrafter,
    NoteInterpreter,
    RunLock,
    TaskSource,
)
from techcom_followup.application.reconcile import ReconcileUnknown
from techcom_followup.application.repository import (
    ActionRecord,
    ActionStatus,
    ActionUpdate,
    ClaimRequest,
    Repository,
)
from techcom_followup.application.retry import RetryBudget, RetryPolicy
from techcom_followup.domain.interpretation import fallback
from techcom_followup.domain.messages import MessageDraft, compose
from techcom_followup.domain.models import (
    Decision,
    NoteInterpretation,
    ScreenResult,
    Task,
)
from techcom_followup.infrastructure.resilience import CircuitBreaker


class Interpreter:
    calls = 0

    async def interpret(
        self, task: Task, *, as_of: date, screen: ScreenResult
    ) -> NoteInterpretation:
        self.calls += 1
        return fallback()


class Drafter:
    calls = 0

    async def draft(
        self, decision: Decision, task: Task, *, as_of: date
    ) -> MessageDraft:
        self.calls += 1
        return compose(decision, task, as_of)


def processor(
    repository: Repository,
    source: TaskSource,
    *,
    lock: RunLock | None = None,
    interpreter: NoteInterpreter | None = None,
    drafter: MessageDrafter | None = None,
) -> ProcessTasks:
    return ProcessTasks(
        ProcessingDependencies(
            repository,
            source,
            repo_policy(),
            FixedClock(NOW),
            lock if lock is not None else InMemoryRunLock(),
        ),
        interpreter=interpreter,
        drafter=drafter,
    )


async def enqueue(repository: Repository) -> ActionRecord:
    await repository.record_decision(await action_write(repository), NOW)
    return (await repository.list_actions())[0]


async def test_dry_run_has_no_persistence_or_llm_side_effects() -> None:
    repository = InMemoryRepository()
    interpreter, drafter = Interpreter(), Drafter()
    report = await processor(
        repository,
        Source(fixture_records()),
        interpreter=interpreter,
        drafter=drafter,
    ).execute(as_of=AS_OF, dry_run=True)
    assert report.status == "dry_run"
    assert report.run_id is None
    assert len(report.decisions) == 10
    assert not repository._runs
    assert not await repository.list_actions()
    assert not await repository.list_audit()
    assert interpreter.calls == drafter.calls == 0


async def test_injected_workflows_and_duplicate_draft_reuse() -> None:
    repository = InMemoryRepository()
    interpreter, drafter = Interpreter(), Drafter()
    usecase = processor(
        repository,
        Source(fixture_records()[:1]),
        interpreter=interpreter,
        drafter=drafter,
    )
    await usecase.execute(as_of=AS_OF)
    report = await usecase.execute(as_of=AS_OF)
    assert report.duplicates == 1
    assert interpreter.calls == 2
    assert drafter.calls == 1


async def test_rejected_records_are_audited_without_raw_notes() -> None:
    repository = InMemoryRepository()
    report = await processor(
        repository, Source([{"task_id": "bad", "notes": "private"}])
    ).execute(as_of=AS_OF)
    assert len(report.rejected) == 1
    audit = await repository.list_audit()
    assert audit[0].kind == "task.rejected"
    assert "private" not in repr(audit)
    assert report.run_id is not None
    run = await repository.get_run(report.run_id)
    assert run is not None
    assert run.stats is not None
    assert run.stats["rejected"] == 1


async def test_dry_run_rejections_are_not_audited() -> None:
    repository = InMemoryRepository()
    report = await processor(repository, Source([None])).execute(
        as_of=AS_OF, dry_run=True
    )
    assert len(report.rejected) == 1
    assert not await repository.list_audit()


async def test_concurrent_runs_are_excluded_and_lock_releases() -> None:
    repository = InMemoryRepository()
    entered, release = asyncio.Event(), asyncio.Event()

    class BlockingSource:
        async def fetch_tasks(self) -> list[object]:
            entered.set()
            await release.wait()
            return fixture_records()[:1]

    lock = InMemoryRunLock()
    usecase = processor(repository, BlockingSource(), lock=lock)
    first = asyncio.create_task(usecase.execute(as_of=AS_OF))
    await entered.wait()
    skipped = await usecase.execute(as_of=AS_OF)
    assert skipped.status == "skipped_locked"
    assert skipped.run_id is None
    assert len(repository._runs) == 1
    release.set()
    assert (await first).enqueued == 1
    assert (await usecase.execute(as_of=AS_OF)).duplicates == 1


async def test_cancelled_run_finishes_failed_and_unlocks() -> None:
    repository = InMemoryRepository()
    entered = asyncio.Event()

    class BlockingSource:
        async def fetch_tasks(self) -> list[object]:
            entered.set()
            await asyncio.sleep(60)
            return []

    lock = InMemoryRunLock()
    usecase = processor(repository, BlockingSource(), lock=lock)
    task = asyncio.create_task(usecase.execute(as_of=AS_OF))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    run = next(iter(repository._runs.values()))
    assert run.stats is not None
    assert run.stats["status"] == "failed"
    async with lock.acquire() as acquired:
        assert acquired


@pytest.mark.parametrize("kind", list(DeliveryKind))
async def test_dispatch_maps_outcomes_and_honors_server_delay(
    kind: DeliveryKind,
) -> None:
    repository = InMemoryRepository()
    await enqueue(repository)
    gateway = Gateway(
        result=DeliveryResult(
            kind,
            "ref" if kind is DeliveryKind.ACCEPTED else None,
            retry_after=10,
        )
    )
    dispatcher = DispatchActions(
        DeliveryDependencies(repository, gateway, FixedClock(NOW)),
        jitter=lambda: 0,
    )
    assert await dispatcher.execute() == 1
    action = (await repository.list_actions())[0]
    assert action.status is delivery_status(gateway.result, 1, RetryPolicy())
    assert action.next_attempt_at == NOW + timedelta(seconds=10)


@pytest.mark.parametrize(
    "kind",
    [DeliveryKind.RETRYABLE, DeliveryKind.PERMANENT, DeliveryKind.UNKNOWN],
)
def test_attempt_cap_never_discards_unknown(kind: DeliveryKind) -> None:
    status = delivery_status(DeliveryResult(kind), 6, RetryPolicy())
    assert status is (
        ActionStatus.UNKNOWN
        if kind is DeliveryKind.UNKNOWN
        else ActionStatus.DEAD
    )


async def test_kill_switch_and_stop_prevent_claims() -> None:
    repository = InMemoryRepository()
    await enqueue(repository)
    gateway = Gateway()
    dispatcher = DispatchActions(
        DeliveryDependencies(repository, gateway, FixedClock(NOW)),
        enabled=lambda: False,
    )
    assert await dispatcher.execute() == 0
    enabled = DispatchActions(
        DeliveryDependencies(repository, gateway, FixedClock(NOW))
    )
    enabled.stop()
    assert await enabled.execute() == 0
    assert not gateway.sends
    assert (await repository.list_actions())[0].attempts == 0


async def test_kill_switch_changed_by_first_send_stops_next_claim() -> None:
    repository = InMemoryRepository()
    await enqueue(repository)
    await repository.record_decision(
        await action_write(repository, "TC-2"), NOW
    )
    allowed = True

    class SwitchGateway(Gateway):
        @override
        async def send(self, action: ActionRecord) -> DeliveryResult:
            nonlocal allowed
            allowed = False
            return await super().send(action)

    gateway = SwitchGateway()
    dispatcher = DispatchActions(
        DeliveryDependencies(repository, gateway, FixedClock(NOW)),
        enabled=lambda: allowed,
    )
    assert await dispatcher.execute() == 1
    assert sum(row.attempts for row in await repository.list_actions()) == 1


async def test_open_circuit_defers_without_counting_an_attempt() -> None:
    repository = InMemoryRepository()
    await enqueue(repository)
    breaker = CircuitBreaker(
        failure_threshold=1, cooldown_s=30, monotonic=lambda: 0
    )
    breaker.record_failure()
    gateway = Gateway()
    dispatcher = DispatchActions(
        DeliveryDependencies(repository, gateway, FixedClock(NOW)),
        breaker=breaker,
    )
    assert await dispatcher.execute() == 0
    action = (await repository.list_actions())[0]
    assert action.status is ActionStatus.PENDING
    assert action.attempts == 0
    assert action.last_error is None
    assert not gateway.sends


@pytest.mark.parametrize(
    "kind",
    [DeliveryKind.ACCEPTED, DeliveryKind.RETRYABLE, DeliveryKind.UNKNOWN],
)
async def test_breaker_records_delivery_health(kind: DeliveryKind) -> None:
    repository = InMemoryRepository()
    await enqueue(repository)
    breaker = CircuitBreaker(
        failure_threshold=1, cooldown_s=30, monotonic=lambda: 0
    )
    gateway = Gateway(result=DeliveryResult(kind))
    dispatcher = DispatchActions(
        DeliveryDependencies(repository, gateway, FixedClock(NOW)),
        breaker=breaker,
    )
    await dispatcher.execute()
    assert breaker.allow() is (kind is DeliveryKind.ACCEPTED)


async def test_retry_budget_defers_unsent_retry_without_increment() -> None:
    repository = InMemoryRepository()
    await enqueue(repository)
    clock = FixedClock(NOW)
    gateway = Gateway(result=DeliveryResult(DeliveryKind.RETRYABLE))
    dispatcher = DispatchActions(
        DeliveryDependencies(repository, gateway, clock), jitter=lambda: 1
    )
    await dispatcher.execute()
    clock.instant += timedelta(seconds=10)
    assert await dispatcher.execute(budget=RetryBudget(0)) == 0
    action = (await repository.list_actions())[0]
    assert action.status is ActionStatus.PENDING
    assert action.attempts == 1
    assert action.last_error == "retry_budget"
    clock.instant += timedelta(seconds=10)
    assert await dispatcher.execute(budget=RetryBudget(5)) == 1
    assert (await repository.list_actions())[0].attempts == 2


async def test_unexpected_send_error_preserves_unknown_and_propagates() -> (
    None
):
    repository = InMemoryRepository()
    await enqueue(repository)

    class BrokenGateway(Gateway):
        @override
        async def send(self, action: ActionRecord) -> DeliveryResult:
            msg = "synthetic bug"
            raise RuntimeError(msg)

    dispatcher = DispatchActions(
        DeliveryDependencies(repository, BrokenGateway(), FixedClock(NOW))
    )
    with pytest.raises(RuntimeError, match="synthetic"):
        await dispatcher.execute()
    assert (await repository.list_actions())[0].status is ActionStatus.UNKNOWN


@pytest.mark.parametrize("kind", list(DeliveryKind))
async def test_reconcile_retains_unknown_unless_found_or_absent(
    kind: DeliveryKind,
) -> None:
    repository = InMemoryRepository()
    await enqueue(repository)
    action = (await repository.claim_actions(ClaimRequest(NOW)))[0]
    assert action.lease_token is not None
    await repository.release_claim(action.id, action.lease_token, NOW)
    gateway = Gateway(lookup_result=DeliveryResult(kind))
    reconciler = ReconcileUnknown(
        DeliveryDependencies(repository, gateway, FixedClock(NOW))
    )
    assert await reconciler.execute() == 1
    status = (await repository.list_actions())[0].status
    expected = {
        DeliveryKind.ACCEPTED: ActionStatus.SENT,
        DeliveryKind.ABSENT: ActionStatus.PENDING,
    }.get(kind, ActionStatus.UNKNOWN)
    assert status is expected
    assert not gateway.sends


async def test_reconcile_absent_after_attempt_cap_goes_dead() -> None:
    repository = InMemoryRepository()
    await enqueue(repository)
    action = (await repository.claim_actions(ClaimRequest(NOW)))[0]
    repository._actions[action.id] = replace(action, attempts=6)
    assert action.lease_token is not None
    await repository.release_claim(action.id, action.lease_token, NOW)
    reconciler = ReconcileUnknown(
        DeliveryDependencies(repository, Gateway(), FixedClock(NOW))
    )
    await reconciler.execute()
    assert (await repository.list_actions())[0].status is ActionStatus.DEAD


async def test_reconcile_cancellation_releases_unknown_claim() -> None:
    repository = InMemoryRepository()
    await enqueue(repository)
    action = (await repository.claim_actions(ClaimRequest(NOW)))[0]
    assert action.lease_token is not None
    await repository.release_claim(action.id, action.lease_token, NOW)

    class BrokenLookup(Gateway):
        @override
        async def lookup(self, operation_id: UUID) -> DeliveryResult:
            raise asyncio.CancelledError

    reconciler = ReconcileUnknown(
        DeliveryDependencies(repository, BrokenLookup(), FixedClock(NOW))
    )
    with pytest.raises(asyncio.CancelledError):
        await reconciler.execute()
    action = (await repository.list_actions())[0]
    assert action.status is ActionStatus.UNKNOWN
    assert action.lease_token is None


@pytest.mark.parametrize("finish", [False, True])
async def test_graceful_shutdown_drains_or_cancels_then_reconciles(
    finish: bool,
) -> None:
    repository = InMemoryRepository()
    await enqueue(repository)
    entered, release = asyncio.Event(), asyncio.Event()

    class SlowGateway(Gateway):
        @override
        async def send(self, action: ActionRecord) -> DeliveryResult:
            entered.set()
            await release.wait()
            return await super().send(action)

    gateway = SlowGateway()
    dispatcher = DispatchActions(
        DeliveryDependencies(repository, gateway, FixedClock(NOW))
    )
    worker = asyncio.create_task(dispatcher.execute())
    await entered.wait()
    if finish:
        release.set()
    await graceful_shutdown(
        worker, stop_claiming=dispatcher.stop, timeout_seconds=0.001
    )
    assert (await repository.list_actions())[0].status is (
        ActionStatus.SENT if finish else ActionStatus.UNKNOWN
    )
    assert await dispatcher.execute() == 0


@pytest.mark.parametrize("grace", [0, -1, 26])
async def test_shutdown_rejects_invalid_deadlines(grace: float) -> None:
    worker = asyncio.create_task(asyncio.sleep(0))
    try:
        with pytest.raises(ValueError, match="grace"):
            await graceful_shutdown(
                worker, stop_claiming=lambda: None, timeout_seconds=grace
            )
    finally:
        await worker


async def test_exact_batch_limit_is_honored() -> None:
    repository = InMemoryRepository()
    await enqueue(repository)
    await repository.record_decision(
        await action_write(repository, "TC-2"), NOW
    )
    gateway = Gateway(result=DeliveryResult(DeliveryKind.UNKNOWN))
    deps = DeliveryDependencies(repository, gateway, FixedClock(NOW))
    assert await DispatchActions(deps).execute(limit=1) == 1
    assert len(gateway.sends) == 1
    clock = FixedClock(NOW + timedelta(seconds=600))
    assert (
        await ReconcileUnknown(replace(deps, clock=clock)).execute(limit=1)
        == 1
    )
    assert len(gateway.lookups) == 1


async def test_malformed_repository_claims_fail_closed() -> None:
    repository = InMemoryRepository()
    action = await enqueue(repository)

    class UnfencedRepository(InMemoryRepository):
        @override
        async def claim_actions(
            self, request: ClaimRequest
        ) -> tuple[ActionRecord, ...]:
            return (action,)

        @override
        async def claim_unknown(
            self, request: ClaimRequest
        ) -> tuple[ActionRecord, ...]:
            return (action,)

    invalid = UnfencedRepository()
    deps = DeliveryDependencies(invalid, Gateway(), FixedClock(NOW))
    with pytest.raises(ValueError, match="unfenced"):
        await DispatchActions(deps).execute(limit=1)
    elapsed = 0
    breaker = CircuitBreaker(
        failure_threshold=1, cooldown_s=30, monotonic=lambda: elapsed
    )
    breaker.record_failure()
    elapsed = 31
    assert breaker.allow()
    with pytest.raises(ValueError, match="unfenced"):
        await DispatchActions(deps, breaker=breaker).execute(limit=1)
    with pytest.raises(ValueError, match="unfenced"):
        await ReconcileUnknown(deps).execute(limit=1)


async def test_late_send_cannot_overwrite_reconciliation() -> None:
    repository = InMemoryRepository()
    await enqueue(repository)
    clock = FixedClock(NOW)

    class LateGateway(Gateway):
        @override
        async def send(self, action: ActionRecord) -> DeliveryResult:
            clock.instant += timedelta(seconds=31)
            return await super().send(action)

    gateway = LateGateway()
    deps = DeliveryDependencies(repository, gateway, clock)
    assert await DispatchActions(deps).execute() == 1
    # The late accepted response was fenced out. A new dispatcher must
    # recover the expired lease into unknown rather than resend it.
    assert await DispatchActions(deps).execute() == 0
    assert (await repository.list_actions())[0].status is ActionStatus.UNKNOWN
    assert len(gateway.sends) == 1


@pytest.mark.parametrize("signal_stop", [True, False])
async def test_stop_while_claiming_defers_without_an_attempt(
    signal_stop: bool,
) -> None:
    allowed = True
    dispatcher: DispatchActions

    class StoppingRepository(InMemoryRepository):
        @override
        async def claim_actions(
            self, request: ClaimRequest
        ) -> tuple[ActionRecord, ...]:
            nonlocal allowed
            claimed = await super().claim_actions(request)
            if signal_stop:
                dispatcher.stop()
            else:
                allowed = False
            return claimed

    repository = StoppingRepository()
    await enqueue(repository)
    gateway = Gateway()
    dispatcher = DispatchActions(
        DeliveryDependencies(repository, gateway, FixedClock(NOW)),
        enabled=lambda: allowed,
    )
    assert await dispatcher.execute() == 0
    action = (await repository.list_actions())[0]
    assert action.status is ActionStatus.PENDING
    assert action.attempts == 0
    assert action.last_error == "dispatch_stopped"
    assert not gateway.sends


async def test_busy_half_open_probe_defers_without_sending() -> None:
    elapsed = 0
    breaker = CircuitBreaker(
        failure_threshold=1, cooldown_s=30, monotonic=lambda: elapsed
    )
    breaker.record_failure()
    elapsed = 31
    assert breaker.allow()
    repository = InMemoryRepository()
    await enqueue(repository)
    gateway = Gateway()
    dispatcher = DispatchActions(
        DeliveryDependencies(repository, gateway, FixedClock(NOW)),
        breaker=breaker,
    )
    assert await dispatcher.execute() == 0
    action = (await repository.list_actions())[0]
    assert action.attempts == 0
    assert action.last_error == "circuit_open"
    assert not gateway.sends


async def test_corrupt_zero_attempt_inflight_cannot_underflow() -> None:
    repository = InMemoryRepository()
    await enqueue(repository)
    action = (await repository.claim_actions(ClaimRequest(NOW)))[0]
    assert action.lease_token is not None
    repository._actions[action.id] = replace(action, attempts=0)
    assert not await repository.defer_claim(
        ActionUpdate(
            action.id, action.lease_token, NOW, ActionStatus.PENDING, NOW
        )
    )


async def test_cancelled_half_open_probe_recovers_after_cooldown() -> None:
    elapsed = 0
    breaker = CircuitBreaker(
        failure_threshold=1, cooldown_s=30, monotonic=lambda: elapsed
    )
    breaker.record_failure()
    elapsed = 31
    entered, release = asyncio.Event(), asyncio.Event()

    class CancelledProbe(Gateway):
        @override
        async def send(self, action: ActionRecord) -> DeliveryResult:
            entered.set()
            await release.wait()
            return await super().send(action)

    repository = InMemoryRepository()
    await enqueue(repository)
    deps = DeliveryDependencies(repository, CancelledProbe(), FixedClock(NOW))
    worker = asyncio.create_task(
        DispatchActions(deps, breaker=breaker).execute()
    )
    await entered.wait()
    worker.cancel()
    with pytest.raises(asyncio.CancelledError):
        await worker
    assert breaker.state == "open"
    elapsed = 62
    assert breaker.allow()
    breaker.record_success()
    assert breaker.state == "closed"
