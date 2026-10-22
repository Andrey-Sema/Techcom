from dataclasses import replace
from datetime import timedelta
from uuid import uuid4

import pytest

from persistence_support import NOW, action_write
from techcom_followup.application.repository import (
    ActionStatus,
    ActionUpdate,
    ClaimRequest,
    Repository,
)


async def test_unsent_defer_restores_attempt_and_fences_replay(
    repository: Repository,
) -> None:
    await repository.record_decision(await action_write(repository), NOW)
    action = (await repository.claim_actions(ClaimRequest(NOW)))[0]
    assert action.lease_token is not None
    update = ActionUpdate(
        action.id,
        action.lease_token,
        NOW,
        ActionStatus.PENDING,
        NOW + timedelta(seconds=10),
        error="retry_budget",
    )
    assert await repository.defer_claim(update)
    deferred = (await repository.list_actions())[0]
    assert deferred.status is ActionStatus.PENDING
    assert deferred.attempts == 0
    assert deferred.next_attempt_at == update.next_attempt_at
    assert deferred.last_error == "retry_budget"
    assert deferred.lease_token is None
    assert not await repository.defer_claim(update)
    assert not await repository.claim_actions(ClaimRequest(NOW))


@pytest.mark.parametrize("invalid", ["token", "expired", "unknown", "missing"])
async def test_defer_rejects_stale_and_ambiguous_claims(
    repository: Repository, invalid: str
) -> None:
    await repository.record_decision(await action_write(repository), NOW)
    action = (await repository.claim_actions(ClaimRequest(NOW)))[0]
    assert action.lease_token is not None
    update = ActionUpdate(
        action.id, action.lease_token, NOW, ActionStatus.PENDING, NOW
    )
    if invalid == "token":
        update = replace(update, lease_token=uuid4())
    elif invalid == "expired":
        update = replace(update, now=NOW + timedelta(seconds=30))
    elif invalid == "missing":
        update = replace(update, action_id=uuid4())
    else:
        await repository.release_claim(action.id, action.lease_token, NOW)
        action = (await repository.claim_unknown(ClaimRequest(NOW)))[0]
        assert action.lease_token is not None
        update = replace(update, lease_token=action.lease_token)
    before = await repository.list_actions()
    assert not await repository.defer_claim(update)
    assert await repository.list_actions() == before


async def test_defer_rejects_another_target_state(
    repository: Repository,
) -> None:
    with pytest.raises(ValueError, match="pending"):
        await repository.defer_claim(
            ActionUpdate(uuid4(), uuid4(), NOW, ActionStatus.SENT, NOW)
        )


async def test_defer_rejects_unbounded_or_sensitive_error_codes(
    repository: Repository,
) -> None:
    with pytest.raises(ValueError, match="closed error code"):
        await repository.defer_claim(
            ActionUpdate(
                uuid4(),
                uuid4(),
                NOW,
                ActionStatus.PENDING,
                NOW,
                error="private upstream response",
            )
        )
