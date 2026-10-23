"""Payload expiry must not buy another draft or another delivery."""

from datetime import date, timedelta

from persistence_support import NOW
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
)
from techcom_followup.application.process_tasks import (
    ProcessingDependencies,
    ProcessTasks,
)
from techcom_followup.application.retention import PruneData, RetentionPolicy
from techcom_followup.domain.messages import MessageDraft, compose
from techcom_followup.domain.models import Decision, Task


class DraftCounter:
    calls = 0

    async def draft(
        self, decision: Decision, task: Task, *, as_of: date
    ) -> MessageDraft:
        self.calls += 1
        return compose(decision, task, as_of)


async def test_pruned_duplicate_skips_drafting_and_creates_no_new_action() -> (
    None
):
    old = NOW - timedelta(days=400)
    clock = FixedClock(old)
    repository = InMemoryRepository()
    drafter = DraftCounter()
    processor = ProcessTasks(
        ProcessingDependencies(
            repository,
            Source(fixture_records()),
            repo_policy(),
            clock,
            InMemoryRunLock(),
        ),
        drafter=drafter,
    )
    first = await processor.execute(as_of=AS_OF)
    assert first.enqueued > 0
    assert drafter.calls == first.enqueued
    gateway = Gateway()
    await DispatchActions(
        DeliveryDependencies(repository, gateway, clock)
    ).execute(limit=100)
    await PruneData(
        repository,
        RetentionPolicy(
            version="test",
            decision_days=90,
            action_days=365,
            audit_days=365,
            batch_size=100,
            max_batches=100,
        ),
        lambda: NOW,
    ).execute()
    assert await repository.list_actions() == ()
    clock.instant = NOW
    replay = await processor.execute(as_of=AS_OF)
    assert replay.enqueued == 0
    assert replay.duplicates == first.enqueued
    assert drafter.calls == first.enqueued
    assert await repository.list_actions() == ()
