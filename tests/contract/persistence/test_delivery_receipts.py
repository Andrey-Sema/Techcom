from uuid import uuid4

import pytest

from persistence_support import NOW, action_write
from techcom_followup.application.repository import (
    MAX_BATCH_SIZE,
    ActionStatus,
    ActionUpdate,
    ClaimRequest,
    DeliveryAuditKind,
    Repository,
)


@pytest.mark.parametrize("kind", ["delivery", "reconciliation"])
@pytest.mark.parametrize("status", [ActionStatus.SENT, ActionStatus.PENDING])
async def test_outcome_receipt_is_owned_and_uses_effective_status(
    repository: Repository, kind: DeliveryAuditKind, status: ActionStatus
) -> None:
    await repository.record_decision(await action_write(repository), NOW)
    (action,) = await repository.claim_actions(ClaimRequest(NOW))
    assert action.lease_token is not None
    update = ActionUpdate(action.id, action.lease_token, NOW, status, NOW)
    before = await repository.list_audit()
    assert (
        await repository.apply_outcome(
            ActionUpdate(action.id, uuid4(), NOW, status, NOW), audit_kind=kind
        )
        is None
    )
    assert await repository.list_audit() == before
    assert await repository.apply_outcome(update, audit_kind=kind) is status
    event = (await repository.list_audit())[-1]
    expected = (
        f"delivery.{status.value}"
        if kind == "delivery"
        else "delivery.reconciled"
        if status is ActionStatus.SENT
        else "delivery.reconciliation"
    )
    assert event.kind == expected
    assert event.task_id == action.decision.task_id
    assert event.payload["status"] == status.value
    assert event.payload["operation_id"] == str(action.identity.operation_id)
    assert await repository.apply_outcome(update, audit_kind=kind) is None
    assert (await repository.list_audit())[-1] == event


async def test_retained_plan_lookup_is_complete_bounded_and_detached(
    repository: Repository,
) -> None:
    assert await repository.get_action_plans(()) == {}
    assert await repository.get_action_plans(("missing",)) == {}
    with pytest.raises(ValueError, match="batch bound"):
        await repository.get_action_plans(
            tuple("key" for _ in range(MAX_BATCH_SIZE + 1))
        )
    write = await action_write(repository)
    await repository.record_decision(write, NOW)
    assert write.identity is not None
    key = write.identity.idempotency_key
    assert await repository.get_action_plans((key,)) == {key: write}
