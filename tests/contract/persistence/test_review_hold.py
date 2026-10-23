"""A held action waits for a person; no automated path sends it."""

from dataclasses import replace

import pytest

from persistence_support import NOW, action_write
from techcom_followup.application.repository import (
    REVIEW_REJECTED,
    ActionStatus,
    ClaimRequest,
    DecisionOutcome,
    DecisionWrite,
    Repository,
)
from techcom_followup.domain.models import Flag


async def held_write(repository: Repository) -> DecisionWrite:
    write = await action_write(repository)
    decision = replace(
        write.decision,
        flags=write.decision.flags | {Flag.HELD_FOR_REVIEW},
    )
    return replace(write, decision=decision)


@pytest.mark.owasp_llm("LLM06")
async def test_held_action_is_never_claimed(repository: Repository) -> None:
    stored = await repository.record_decision(
        await held_write(repository), NOW
    )
    assert stored.outcome is DecisionOutcome.ENQUEUED
    (action,) = await repository.list_actions()
    assert action.status is ActionStatus.HELD
    assert await repository.claim_actions(ClaimRequest(NOW)) == ()
    assert await repository.claim_unknown(ClaimRequest(NOW)) == ()
    summary = {
        item.status: item.count for item in await repository.outbox_summary()
    }
    assert summary == {ActionStatus.HELD: 1}


async def test_approval_releases_the_same_operation_once(
    repository: Repository,
) -> None:
    await repository.record_decision(await held_write(repository), NOW)
    (held,) = await repository.list_actions()
    operation = held.identity.operation_id
    approved = await repository.review_held(operation, approve=True, now=NOW)
    assert approved is not None
    assert approved.status is ActionStatus.PENDING
    assert approved.identity == held.identity
    assert (
        await repository.review_held(operation, approve=True, now=NOW) is None
    )
    (claimed,) = await repository.claim_actions(ClaimRequest(NOW))
    assert claimed.identity.operation_id == operation
    kinds = [event.kind for event in await repository.list_audit()]
    assert kinds.count("action.review_approved") == 1


async def test_rejection_is_terminal(repository: Repository) -> None:
    await repository.record_decision(await held_write(repository), NOW)
    (held,) = await repository.list_actions()
    rejected = await repository.review_held(
        held.identity.operation_id, approve=False, now=NOW
    )
    assert rejected is not None
    assert rejected.status is ActionStatus.DEAD
    assert rejected.last_error == REVIEW_REJECTED
    assert rejected.terminal_at is not None
    assert await repository.claim_actions(ClaimRequest(NOW)) == ()
    events = await repository.list_audit()
    (review,) = [e for e in events if e.kind == "action.review_rejected"]
    assert review.payload == {
        "action_id": str(held.id),
        "operation_id": str(held.identity.operation_id),
    }
    assert review.task_id == held.decision.task_id


async def test_only_held_actions_can_be_reviewed(
    repository: Repository,
) -> None:
    await repository.record_decision(await action_write(repository), NOW)
    (pending,) = await repository.list_actions()
    assert pending.status is ActionStatus.PENDING
    for approve in (True, False):
        assert (
            await repository.review_held(
                pending.identity.operation_id, approve=approve, now=NOW
            )
            is None
        )
    (unchanged,) = await repository.list_actions()
    assert unchanged.status is ActionStatus.PENDING
