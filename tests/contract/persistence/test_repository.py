import asyncio
from dataclasses import replace
from datetime import datetime, timedelta
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest

from persistence_support import NOW, action_write
from support import AS_OF, make_task, repo_policy
from techcom_followup.application.repository import (
    ActionRecord,
    ActionStatus,
    ActionUpdate,
    AuditEvent,
    CacheEntry,
    ClaimRequest,
    DecisionOutcome,
    DecisionWrite,
    Repository,
)
from techcom_followup.domain.interpretation import fallback

if TYPE_CHECKING:
    from pydantic import JsonValue


async def enqueue(
    repository: Repository, task_id: str = "TC-1"
) -> ActionRecord:
    write = await action_write(repository, task_id)
    await repository.record_decision(write, NOW)
    return (await repository.list_actions())[-1]


def outcome(
    action: ActionRecord, status: ActionStatus, now: datetime = NOW
) -> ActionUpdate:
    assert action.lease_token is not None
    return ActionUpdate(action.id, action.lease_token, now, status, now)


async def test_run_lifecycle_and_detached_stats(
    repository: Repository,
) -> None:
    run = await repository.start_run(AS_OF, "2026-10-21.1", NOW)
    assert run.as_of == AS_OF
    assert run.started_at == NOW
    assert run.stats == {}
    if isinstance(run.stats, dict):
        run.stats["caller_mutation"] = True
    pristine = await repository.get_run(run.id)
    assert pristine is not None
    assert pristine.stats == {}
    assert await repository.get_run(uuid4()) is None
    snapshot = await repository.get_run(run.id)
    assert snapshot is not None
    assert snapshot.id == run.id
    stats = {"enqueued": 2}
    assert await repository.finish_run(run.id, stats, NOW)
    stats["enqueued"] = 999
    finished = await repository.get_run(run.id)
    assert finished is not None
    assert finished.stats == {"enqueued": 2}
    assert not await repository.finish_run(run.id, {}, NOW)
    assert not await repository.finish_run(uuid4(), {}, NOW)


async def test_atomic_enqueue_and_replay(repository: Repository) -> None:
    write = await action_write(repository)
    stored = await repository.record_decision(write, NOW)
    assert stored.outcome == DecisionOutcome.ENQUEUED
    assert await repository.record_decision(write, NOW) == stored
    assert await repository.list_decisions(write.run_id) == (stored,)
    assert await repository.list_decisions(uuid4()) == ()
    actions = await repository.list_actions()
    assert len(actions) == 1
    assert actions[0].identity == write.identity
    assert actions[0].decision == write.decision
    assert actions[0].message == write.message
    assert actions[0].attempts == 0
    assert actions[0].status == ActionStatus.PENDING
    assert len(await repository.list_audit()) == 1


async def test_two_runs_concurrently_dedupe_one_action(
    repository: Repository,
) -> None:
    writes = [await action_write(repository) for _ in range(5)]
    stored = await asyncio.gather(
        *(repository.record_decision(write, NOW) for write in writes)
    )
    assert sum(row.outcome == DecisionOutcome.ENQUEUED for row in stored) == 1
    assert (
        sum(
            row.outcome == DecisionOutcome.DUPLICATE_SUPPRESSED
            for row in stored
        )
        == 4
    )
    assert len(await repository.list_actions()) == 1
    assert len(await repository.list_audit()) == 5


async def test_same_run_task_race_creates_one_decision(
    repository: Repository,
) -> None:
    write = await action_write(repository)
    stored = await asyncio.gather(
        *(repository.record_decision(write, NOW) for _ in range(4))
    )
    assert len({row.id for row in stored}) == 1
    assert len(await repository.list_decisions(write.run_id)) == 1
    assert len(await repository.list_actions()) == 1
    assert len(await repository.list_audit()) == 1


async def test_no_action_never_creates_outbox(repository: Repository) -> None:
    run = await repository.start_run(AS_OF, "2026-10-21.1", NOW)
    decision = repo_policy().decide(make_task(), as_of=AS_OF)
    stored = await repository.record_decision(
        DecisionWrite(run.id, decision, "fp"), NOW
    )
    assert stored.outcome == DecisionOutcome.NONE
    assert await repository.list_actions() == ()


async def test_failed_write_leaves_nothing(repository: Repository) -> None:
    write = await action_write(repository)
    with pytest.raises(ValueError, match="run does not exist"):
        await repository.record_decision(replace(write, run_id=uuid4()), NOW)
    with pytest.raises(ValueError, match="identity and message"):
        await repository.record_decision(replace(write, message=None), NOW)
    assert await repository.list_actions() == ()
    assert await repository.list_audit() == ()
    assert await repository.list_decisions(write.run_id) == ()


async def test_parallel_dispatchers_claim_disjoint_batches(
    repository: Repository,
) -> None:
    for index in range(6):
        await enqueue(repository, f"TC-{index}")
    first, second = await asyncio.gather(
        repository.claim_actions(ClaimRequest(NOW, limit=3)),
        repository.claim_actions(ClaimRequest(NOW, limit=3)),
    )
    assert len(first) == len(second) == 3
    assert {row.id for row in first}.isdisjoint(row.id for row in second)
    assert all(row.attempts == 1 for row in (*first, *second))
    assert await repository.claim_actions(ClaimRequest(NOW)) == ()
    assert await repository.claim_unknown(ClaimRequest(NOW)) == ()


@pytest.mark.parametrize("terminal", [ActionStatus.SENT, ActionStatus.DEAD])
async def test_terminal_rows_never_leave_terminal(
    repository: Repository, terminal: ActionStatus
) -> None:
    await enqueue(repository)
    action = (await repository.claim_actions(ClaimRequest(NOW)))[0]
    update = replace(outcome(action, terminal), provider_ref="receipt")
    assert await repository.apply_outcome(update)
    assert not await repository.apply_outcome(
        outcome(action, ActionStatus.PENDING)
    )
    current = (await repository.list_actions())[0]
    assert current.status == terminal
    assert current.sent_at == (NOW if terminal == ActionStatus.SENT else None)
    assert current.provider_ref == "receipt"
    assert current.lease_token is None
    assert (
        await repository.claim_actions(ClaimRequest(NOW + timedelta(days=1)))
        == ()
    )


async def test_retry_is_not_claimed_until_scheduled(
    repository: Repository,
) -> None:
    await enqueue(repository)
    action = (await repository.claim_actions(ClaimRequest(NOW)))[0]
    retry_at = NOW + timedelta(minutes=2)
    assert await repository.apply_outcome(
        replace(
            outcome(action, ActionStatus.PENDING),
            next_attempt_at=retry_at,
            error="rate_limited",
        )
    )
    assert await repository.claim_actions(ClaimRequest(NOW)) == ()
    retry = (await repository.claim_actions(ClaimRequest(retry_at)))[0]
    assert retry.attempts == 2
    assert retry.identity == action.identity
    assert retry.last_error == "rate_limited"
    assert retry.lease_token != action.lease_token
    assert not await repository.apply_outcome(
        outcome(action, ActionStatus.SENT, retry_at)
    )


async def test_unknown_requires_lookup_before_resend(
    repository: Repository,
) -> None:
    await enqueue(repository)
    action = (await repository.claim_actions(ClaimRequest(NOW)))[0]
    assert await repository.apply_outcome(
        outcome(action, ActionStatus.UNKNOWN)
    )
    assert await repository.claim_actions(ClaimRequest(NOW)) == ()
    lookup = (await repository.claim_unknown(ClaimRequest(NOW)))[0]
    assert lookup.status == ActionStatus.UNKNOWN
    assert lookup.attempts == 1
    assert await repository.claim_unknown(ClaimRequest(NOW)) == ()
    assert await repository.apply_outcome(
        outcome(lookup, ActionStatus.PENDING)
    )
    resend = (await repository.claim_actions(ClaimRequest(NOW)))[0]
    assert resend.identity == action.identity
    assert resend.attempts == 2


async def test_expired_send_is_unknown_and_old_worker_is_fenced(
    repository: Repository,
) -> None:
    await enqueue(repository)
    first = (await repository.claim_actions(ClaimRequest(NOW)))[0]
    expired = NOW + timedelta(seconds=30)
    assert not await repository.apply_outcome(
        outcome(first, ActionStatus.SENT, expired)
    )
    assert await repository.claim_actions(ClaimRequest(expired)) == ()
    current = (await repository.list_actions())[0]
    assert current.status == ActionStatus.UNKNOWN
    assert current.last_error == "lease_expired"
    lookup = (await repository.claim_unknown(ClaimRequest(expired)))[0]
    assert lookup.lease_token != first.lease_token
    assert lookup.attempts == 1
    assert first.lease_token is not None
    assert not await repository.release_claim(
        first.id, first.lease_token, expired
    )
    assert await repository.apply_outcome(
        outcome(lookup, ActionStatus.SENT, expired)
    )


async def test_expired_reconciliation_claim_is_fenced(
    repository: Repository,
) -> None:
    await enqueue(repository)
    send = (await repository.claim_actions(ClaimRequest(NOW)))[0]
    assert send.lease_token is not None
    assert await repository.release_claim(send.id, send.lease_token, NOW)
    first = (await repository.claim_unknown(ClaimRequest(NOW)))[0]
    later = NOW + timedelta(seconds=31)
    second = (await repository.claim_unknown(ClaimRequest(later)))[0]
    assert second.lease_token != first.lease_token
    assert not await repository.apply_outcome(
        outcome(first, ActionStatus.SENT, later)
    )
    assert await repository.apply_outcome(
        outcome(second, ActionStatus.SENT, later)
    )


async def test_nonowners_cannot_mutate_claim(repository: Repository) -> None:
    await enqueue(repository)
    pending = (await repository.list_actions())[0]
    assert not await repository.release_claim(pending.id, uuid4(), NOW)
    assert not await repository.release_claim(uuid4(), uuid4(), NOW)
    action = (await repository.claim_actions(ClaimRequest(NOW)))[0]
    assert not await repository.release_claim(action.id, uuid4(), NOW)
    assert action.lease_token is not None
    assert await repository.release_claim(action.id, action.lease_token, NOW)
    unknown = (await repository.list_actions())[0]
    assert unknown.status == ActionStatus.UNKNOWN
    assert unknown.attempts == 1


async def test_cache_upsert_expiry_boundary(repository: Repository) -> None:
    entry = CacheEntry(
        "key", "model", "interp.v1", fallback(), NOW, NOW + timedelta(days=30)
    )
    assert await repository.get_cached_llm("absent", NOW) is None
    await repository.put_cached_llm(entry)
    assert await repository.get_cached_llm("key", NOW) == entry
    updated = replace(entry, prompt_version="interp.v2")
    await repository.put_cached_llm(updated)
    assert await repository.get_cached_llm("key", NOW) == updated
    assert await repository.get_cached_llm("key", entry.expires_at) is None


async def test_audit_snapshots_do_not_allow_nested_mutations(
    repository: Repository,
) -> None:
    counts: list[JsonValue] = [1, 2]
    payload: dict[str, JsonValue] = {"metadata": {"counts": counts}}
    event = AuditEvent("test.event", NOW, payload)
    await repository.append_audit(event)
    counts.append(3)
    rows = await repository.list_audit()
    assert rows[0].payload == {"metadata": {"counts": [1, 2]}}
    assert rows[0].kind == "test.event"


@pytest.mark.parametrize(
    "claim_request",
    [ClaimRequest(NOW, limit=0), ClaimRequest(NOW, lease=timedelta(0))],
)
async def test_invalid_claim_is_rejected(
    repository: Repository, claim_request: ClaimRequest
) -> None:
    with pytest.raises(ValueError, match="positive lease"):
        await repository.claim_actions(claim_request)
