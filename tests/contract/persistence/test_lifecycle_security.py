"""Retention must not remove authority, active work or duplicate identity."""

from dataclasses import replace
from datetime import timedelta

from persistence_support import NOW, action_write
from support import AS_OF
from techcom_followup.application.repository import (
    ActionStatus,
    ActionUpdate,
    ClaimRequest,
    DecisionOutcome,
    Repository,
)
from techcom_followup.application.retention import PruneData, RetentionPolicy

OLD = NOW - timedelta(days=400)
POLICY = RetentionPolicy(
    version="lifecycle.review.v1",
    decision_days=90,
    action_days=365,
    audit_days=365,
    batch_size=10,
    max_batches=100,
)


async def test_replay_after_payload_retention_is_still_suppressed(
    repository: Repository,
) -> None:
    write = await action_write(repository)
    old_run = await repository.start_run(AS_OF, "v", OLD)
    await repository.record_decision(replace(write, run_id=old_run.id), OLD)
    (action,) = await repository.claim_actions(ClaimRequest(OLD, limit=1))
    assert action.lease_token is not None
    await repository.apply_outcome(
        ActionUpdate(
            action.id,
            action.lease_token,
            OLD,
            ActionStatus.SENT,
            OLD,
        )
    )
    await repository.finish_run(old_run.id, {"status": "completed"}, OLD)
    await PruneData(repository, POLICY, lambda: NOW).execute()
    assert not await repository.list_actions()
    new_run = await repository.start_run(AS_OF, "v", NOW)
    saved = await repository.record_decision(
        replace(write, run_id=new_run.id), NOW
    )
    assert saved.outcome == DecisionOutcome.DUPLICATE_SUPPRESSED
    assert not await repository.list_actions()


async def test_active_empty_run_and_recently_finished_old_run_survive(
    repository: Repository,
) -> None:
    active = await repository.start_run(AS_OF, "v", OLD)
    recent = await repository.start_run(AS_OF, "v", OLD)
    await repository.finish_run(recent.id, {"status": "completed"}, NOW)
    await PruneData(repository, POLICY, lambda: NOW).execute()
    assert await repository.get_run(active.id) is not None
    assert await repository.get_run(recent.id) is not None


async def test_old_pending_action_that_dies_today_keeps_full_terminal_retention(
    repository: Repository,
) -> None:
    write = await action_write(repository)
    await repository.record_decision(write, OLD)
    (action,) = await repository.claim_actions(ClaimRequest(NOW, limit=1))
    assert action.lease_token is not None
    await repository.apply_outcome(
        ActionUpdate(
            action.id,
            action.lease_token,
            NOW,
            ActionStatus.DEAD,
            OLD + timedelta(seconds=1),
        )
    )
    await PruneData(repository, POLICY, lambda: NOW).execute()
    (kept,) = await repository.list_actions()
    assert kept.status == ActionStatus.DEAD
    assert kept.terminal_at == NOW
