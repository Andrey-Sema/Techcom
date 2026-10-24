"""A newer snapshot retires unsent work, preserving uncertain sends."""

from dataclasses import replace

import pytest

from persistence_support import NOW, action_write
from support import AS_OF
from techcom_followup.application.repository import (
    ActionStatus,
    ActionUpdate,
    ClaimRequest,
    DecisionWrite,
    Repository,
)
from techcom_followup.domain.identity import ActionIdentity
from techcom_followup.domain.models import DecisionKind, Flag, ReasonCode


async def close_task(
    repository: Repository, write: DecisionWrite, *, same_state: bool = False
) -> None:
    run = await repository.start_run(AS_OF, "closed", NOW)
    decision = replace(
        write.decision,
        kind=DecisionKind.NO_ACTION,
        target=None,
        reason_code=ReasonCode.COMPLETED,
        reason="Task is done.",
        flags=frozenset(),
    )
    await repository.record_decision(
        DecisionWrite(
            run.id,
            decision,
            write.fingerprint if same_state else "closed-state",
            source_project=write.source_project,
        ),
        NOW,
    )


@pytest.mark.parametrize("held", [False, True])
@pytest.mark.parametrize("same_state", [False, True])
async def test_done_cancels_pending_and_held_actions(
    repository: Repository, held: bool, same_state: bool
) -> None:
    write = await action_write(repository)
    if held:
        write = replace(
            write,
            decision=replace(
                write.decision, flags=frozenset({Flag.HELD_FOR_REVIEW})
            ),
        )
    await repository.record_decision(write, NOW)
    await close_task(repository, write, same_state=same_state)
    (action,) = await repository.list_actions()
    assert action.status is ActionStatus.DEAD
    assert action.last_error == "source_state_superseded"
    assert action.terminal_at is not None
    assert await repository.claim_actions(ClaimRequest(NOW)) == ()
    assert (
        await repository.review_held(
            action.identity.operation_id, approve=True, now=NOW
        )
        is None
    )


@pytest.mark.parametrize("committed", [False, True])
async def test_uncertain_delivery_is_reconciled_but_never_retried_after_done(
    repository: Repository, committed: bool
) -> None:
    write = await action_write(repository)
    await repository.record_decision(write, NOW)
    (claimed,) = await repository.claim_actions(ClaimRequest(NOW))
    assert claimed.lease_token is not None
    await close_task(repository, write)
    assert await repository.release_claim(claimed.id, claimed.lease_token, NOW)
    (uncertain,) = await repository.claim_unknown(ClaimRequest(NOW))
    assert uncertain.lease_token is not None
    assert await repository.apply_outcome(
        ActionUpdate(
            uncertain.id,
            uncertain.lease_token,
            NOW,
            ActionStatus.SENT if committed else ActionStatus.PENDING,
            NOW,
            provider_ref="receipt" if committed else None,
        )
    )
    (finished,) = await repository.list_actions()
    assert finished.status is (
        ActionStatus.SENT if committed else ActionStatus.DEAD
    )
    assert await repository.claim_actions(ClaimRequest(NOW)) == ()


async def test_deferred_claim_cannot_revive_an_obsolete_action(
    repository: Repository,
) -> None:
    write = await action_write(repository)
    await repository.record_decision(write, NOW)
    (claimed,) = await repository.claim_actions(ClaimRequest(NOW))
    assert claimed.lease_token is not None
    await close_task(repository, write)
    assert await repository.defer_claim(
        ActionUpdate(
            claimed.id, claimed.lease_token, NOW, ActionStatus.PENDING, NOW
        )
    )
    (finished,) = await repository.list_actions()
    assert finished.status is ActionStatus.DEAD
    assert finished.attempts == 0


async def test_new_task_state_retires_old_action_but_keeps_new_one(
    repository: Repository,
) -> None:
    write = await action_write(repository)
    await repository.record_decision(write, NOW)
    # A new source snapshot has its own logical action ID.
    newer = await action_write(repository)
    newer = replace(newer, fingerprint="new-state")
    newer = replace(
        newer, identity=ActionIdentity.of(newer.decision, newer.fingerprint)
    )
    await repository.record_decision(newer, NOW)
    actions = await repository.list_actions()
    assert [action.status for action in actions] == [
        ActionStatus.DEAD,
        ActionStatus.PENDING,
    ]
    (claimed,) = await repository.claim_actions(ClaimRequest(NOW))
    assert claimed.identity == newer.identity


async def test_same_id_in_another_project_does_not_cancel_work(
    repository: Repository,
) -> None:
    write = await action_write(repository)
    await repository.record_decision(write, NOW)
    await close_task(repository, replace(write, source_project="Indiana OSP"))
    (action,) = await repository.list_actions()
    assert action.status is ActionStatus.PENDING


@pytest.mark.parametrize("label", [None, "Indiana OSP"])
async def test_done_without_project_authority_cancels_its_own_work(
    repository: Repository, label: str | None
) -> None:
    # Unknown or noncanonical projects carry no source authority; their
    # recipient-less Done decision is scoped by the task label instead.
    write = await action_write(repository)
    write = replace(write, source_project=None)
    target = write.decision.target
    assert target is not None
    await repository.record_decision(write, NOW)
    run = await repository.start_run(AS_OF, "closed", NOW)
    await repository.record_decision(
        DecisionWrite(
            run.id,
            replace(
                write.decision,
                kind=DecisionKind.NO_ACTION,
                target=None,
                reason_code=ReasonCode.COMPLETED,
                reason="Task is done.",
            ),
            "closed-state",
            task_project=label or target.project,
        ),
        NOW,
    )
    (action,) = await repository.list_actions()
    expected = ActionStatus.PENDING if label else ActionStatus.DEAD
    assert action.status is expected
