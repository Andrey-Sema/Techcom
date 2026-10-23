"""Bounded lookups tell new work from retained identity."""

from dataclasses import replace
from datetime import timedelta

import pytest

from persistence_support import NOW, action_write
from support import AS_OF
from techcom_followup.application.repository import (
    ActionStatus,
    ActionUpdate,
    ClaimRequest,
    Repository,
)
from techcom_followup.application.retention import PruneData, RetentionPolicy


async def test_lookup_returns_requested_identity_and_survives_pruning(
    repository: Repository,
) -> None:
    old = NOW - timedelta(days=400)
    write = await action_write(repository)
    assert write.identity is not None
    run = await repository.start_run(AS_OF, "v", old)
    await repository.record_decision(replace(write, run_id=run.id), old)
    await repository.finish_run(run.id, {"status": "completed"}, old)
    key = write.identity.idempotency_key
    assert await repository.get_action_messages((key, "unknown")) == {
        key: write.message
    }
    assert await repository.get_action_messages(()) == {}
    (claimed,) = await repository.claim_actions(ClaimRequest(old, limit=1))
    assert claimed.lease_token is not None
    await repository.apply_outcome(
        ActionUpdate(
            claimed.id,
            claimed.lease_token,
            old,
            ActionStatus.SENT,
            old,
        )
    )
    await PruneData(
        repository,
        RetentionPolicy(
            version="2026-10-23.1",
            decision_days=90,
            action_days=365,
            audit_days=365,
            batch_size=10,
            max_batches=100,
        ),
        lambda: NOW,
    ).execute()
    assert await repository.list_run_actions(run.id) == ()
    assert await repository.get_action_messages((key,)) == {key: None}


async def test_message_lookup_refuses_unbounded_key_lists(
    repository: Repository,
) -> None:
    with pytest.raises(ValueError, match="batch bound"):
        await repository.get_action_messages(("x",) * 1001)
