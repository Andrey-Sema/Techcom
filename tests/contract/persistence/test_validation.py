from dataclasses import replace
from datetime import timedelta
from uuid import uuid4

import pytest
from hypothesis import given
from hypothesis import strategies as st

from persistence_support import NOW, action_write
from support import AS_OF, make_task, repo_policy
from techcom_followup.adapters.outbound.in_memory_repository import (
    InMemoryRepository,
)
from techcom_followup.application.repository import (
    ActionStatus,
    ActionUpdate,
    CacheEntry,
    ClaimRequest,
    DecisionWrite,
    require_aware,
)
from techcom_followup.domain.identity import ActionIdentity
from techcom_followup.domain.interpretation import fallback


def test_naive_clock_is_rejected() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        require_aware(NOW.replace(tzinfo=None))


@pytest.mark.parametrize("days", [0, -1])
def test_cache_needs_finite_positive_lifetime(days: int) -> None:
    entry = CacheEntry(
        "key", "model", "v1", fallback(), NOW, NOW + timedelta(days=days)
    )
    with pytest.raises(ValueError, match="expiry"):
        entry.validate()


@pytest.mark.parametrize("confidence", [float("nan"), float("inf"), -0.1, 1.1])
def test_cache_rejects_invalid_interpretation(confidence: float) -> None:
    entry = CacheEntry(
        "key",
        "model",
        "v1",
        replace(fallback(), confidence=confidence),
        NOW,
        NOW + timedelta(days=1),
    )
    with pytest.raises(ValueError, match="validated bounds"):
        entry.validate()


def test_cache_rejects_overlong_evidence() -> None:
    entry = CacheEntry(
        "key",
        "model",
        "v1",
        replace(fallback(), evidence="x" * 201),
        NOW,
        NOW + timedelta(days=1),
    )
    with pytest.raises(ValueError, match="validated bounds"):
        entry.validate()


@pytest.mark.parametrize(
    ("status", "error"),
    [(ActionStatus.IN_FLIGHT, None), (ActionStatus.PENDING, "x" * 201)],
)
def test_invalid_outcome_is_rejected(
    status: ActionStatus, error: str | None
) -> None:
    update = ActionUpdate(uuid4(), uuid4(), NOW, status, NOW, error)
    with pytest.raises(ValueError, match=r"claiming|errors"):
        update.validate()


async def test_identity_cannot_be_forged() -> None:
    repository = InMemoryRepository()
    write = await action_write(repository)
    forged = replace(write, identity=ActionIdentity("sha256:forged", uuid4()))
    with pytest.raises(ValueError, match="identity does not match"):
        await repository.record_decision(forged, NOW)
    assert await repository.list_actions() == ()


async def test_quiet_decision_cannot_smuggle_action_fields() -> None:
    repository = InMemoryRepository()
    actionable = await action_write(repository)
    quiet = repo_policy().decide(make_task(), as_of=AS_OF)
    for identity, message in (
        (actionable.identity, None),
        (None, actionable.message),
        (actionable.identity, actionable.message),
    ):
        write = DecisionWrite(
            actionable.run_id, quiet, "fp", identity, message
        )
        with pytest.raises(
            ValueError, match=r"cannot carry|identity and message"
        ):
            write.validate()


@given(st.integers(min_value=1, max_value=3600))
async def test_lease_expiry_always_fences_old_delivery(seconds: int) -> None:
    repository = InMemoryRepository()
    write = await action_write(repository)
    await repository.record_decision(write, NOW)
    lease = timedelta(seconds=seconds)
    first = (await repository.claim_actions(ClaimRequest(NOW, lease=lease)))[0]
    assert first.lease_token is not None
    expiry = NOW + lease
    update = ActionUpdate(
        first.id, first.lease_token, expiry, ActionStatus.SENT, expiry
    )
    assert not await repository.apply_outcome(update)
    assert await repository.claim_actions(ClaimRequest(expiry)) == ()
    unknown = (await repository.claim_unknown(ClaimRequest(expiry)))[0]
    assert unknown.identity == first.identity
    assert unknown.lease_token != first.lease_token
    assert unknown.attempts == 1
