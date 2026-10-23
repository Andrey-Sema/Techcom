"""Retention contract: both repositories delete the same rows."""

from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from persistence_support import NOW, action_write
from support import AS_OF, make_task, repo_policy
from techcom_followup.application.repository import (
    ActionStatus,
    ActionUpdate,
    AuditEvent,
    CacheEntry,
    ClaimRequest,
    DecisionWrite,
    Repository,
)
from techcom_followup.application.retention import PruneData, RetentionPolicy
from techcom_followup.domain.identity import state_fingerprint
from techcom_followup.domain.interpretation import fallback

OLD = NOW - timedelta(days=400)
POLICY = RetentionPolicy(
    version="2026-10-22.1",
    decision_days=90,
    action_days=365,
    audit_days=365,
    batch_size=1,
    max_batches=100,
)


async def enqueue(repository: Repository, task_id: str, at: datetime) -> None:
    write = await action_write(repository, task_id)
    run = await repository.start_run(AS_OF, "v", at)
    await repository.record_decision(replace(write, run_id=run.id), at)


async def send_all(repository: Repository, at: datetime) -> None:
    for claimed in await repository.claim_actions(ClaimRequest(at)):
        assert claimed.lease_token is not None
        await repository.apply_outcome(
            ActionUpdate(
                claimed.id,
                claimed.lease_token,
                at,
                ActionStatus.SENT,
                at,
                provider_ref="mock",
            )
        )


async def no_action(repository: Repository, at: datetime) -> None:
    task = make_task(task_id="TC-90")
    decision = repo_policy().decide(task, as_of=AS_OF)
    run = await repository.start_run(AS_OF, "v", at)
    await repository.record_decision(
        DecisionWrite(run.id, decision, state_fingerprint(task)), at
    )


def cache(key: str, expires_at: datetime) -> CacheEntry:
    return CacheEntry(key, "stub.v1", "interp.v1", fallback(), OLD, expires_at)


@pytest.mark.asvs("14.2.4")
async def test_retention_keeps_work_in_progress(
    repository: Repository,
) -> None:
    await enqueue(repository, "TC-1", OLD)
    await send_all(repository, OLD)  # sent a year ago: deleted
    await enqueue(repository, "TC-3", NOW)
    await send_all(repository, NOW)  # sent today: kept
    await enqueue(repository, "TC-2", OLD)  # still pending: kept
    await no_action(repository, OLD)  # old, no action: deleted
    await repository.put_cached_llm(cache("expired", NOW))
    await repository.put_cached_llm(cache("fresh", NOW + timedelta(days=1)))
    await repository.append_audit(AuditEvent("old.event", OLD, {}))

    total, complete = await PruneData(
        repository, POLICY, lambda: NOW
    ).execute()

    assert complete
    assert (total.cache, total.actions, total.decisions, total.runs) == (
        1,
        1,
        2,
        2,
    )
    # Every audit row from a year ago went, today's stayed.
    assert all(event.at >= NOW for event in await repository.list_audit())
    assert total.audit >= 1
    remaining = {
        (action.decision.task_id, action.status)
        for action in await repository.list_actions()
    }
    assert remaining == {
        ("TC-2", ActionStatus.PENDING),
        ("TC-3", ActionStatus.SENT),
    }
    assert await repository.get_cached_llm("fresh", NOW) is not None
    # A second pass finds nothing left.
    again, _ = await PruneData(repository, POLICY, lambda: NOW).execute()
    assert again.total == 0


async def test_batches_stop_at_the_configured_maximum(
    repository: Repository,
) -> None:
    for index in range(3):
        await repository.append_audit(AuditEvent(f"e{index}", OLD, {}))
    policy = POLICY.model_copy(update={"max_batches": 2})
    total, complete = await PruneData(
        repository, policy, lambda: NOW
    ).execute()
    assert (total.audit, complete) == (2, False)
    rest, complete = await PruneData(repository, policy, lambda: NOW).execute()
    assert (rest.audit, complete) == (1, True)
