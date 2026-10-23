"""Dedupe survives classifier changes and payload pruning."""

import asyncio
from dataclasses import replace
from datetime import timedelta

from persistence_support import NOW, action_write
from support import AS_OF, make_task, repo_policy
from techcom_followup.application.repository import (
    ActionStatus,
    ActionUpdate,
    ClaimRequest,
    DecisionOutcome,
    DecisionWrite,
    Repository,
    RetentionCutoffs,
)
from techcom_followup.domain.identity import ActionIdentity
from techcom_followup.domain.messages import compose
from techcom_followup.domain.models import (
    BlockerCategory,
    DecisionKind,
    ReasonCode,
)


async def alternative(
    repository: Repository,
    *,
    project: str | None = None,
) -> DecisionWrite:
    write = await action_write(repository)
    target = repo_policy().routing.team_for(
        BlockerCategory.MATERIALS,
        project or "SURF Illinois",
    )
    decision = replace(
        write.decision,
        kind=DecisionKind.REQUEST_ETA,
        target=target,
        reason_code=ReasonCode.MATERIALS_ETA_UNKNOWN,
    )
    return replace(
        write,
        decision=decision,
        identity=ActionIdentity.of(decision, write.fingerprint),
        message=compose(decision, make_task(), AS_OF),
        source_project=project or write.source_project,
    )


async def test_different_model_actions_share_one_source_slot(
    repository: Repository,
) -> None:
    original = await action_write(repository)
    changed = await alternative(repository)
    results = await asyncio.gather(
        repository.record_decision(original, NOW),
        repository.record_decision(changed, NOW),
    )
    assert sorted(item.outcome.value for item in results) == [
        "duplicate_suppressed",
        "enqueued",
    ]
    assert len(await repository.list_actions()) == 1


async def test_source_slot_survives_pruning_and_changed_model_identity(
    repository: Repository,
) -> None:
    old = NOW - timedelta(days=400)
    original = await action_write(repository)
    await repository.record_decision(original, old)
    (claimed,) = await repository.claim_actions(ClaimRequest(old))
    assert claimed.lease_token is not None
    await repository.apply_outcome(
        ActionUpdate(
            claimed.id, claimed.lease_token, old, ActionStatus.SENT, old
        )
    )
    await repository.prune(RetentionCutoffs(NOW, NOW, NOW, NOW), 100)
    assert await repository.list_actions() == ()
    replay = await repository.record_decision(
        await alternative(repository), NOW
    )
    assert replay.outcome is DecisionOutcome.DUPLICATE_SUPPRESSED
    assert await repository.list_actions() == ()


async def test_real_state_change_still_allows_a_notification(
    repository: Repository,
) -> None:
    original = await action_write(repository)
    await repository.record_decision(original, NOW)
    changed = await alternative(repository)
    changed = replace(
        changed,
        fingerprint="new-source-state",
        identity=ActionIdentity.of(changed.decision, "new-source-state"),
    )
    result = await repository.record_decision(changed, NOW)
    assert result.outcome is DecisionOutcome.ENQUEUED


async def test_project_is_part_of_the_notification_slot(
    repository: Repository,
) -> None:
    await repository.record_decision(await action_write(repository), NOW)
    result = await repository.record_decision(
        await alternative(repository, project="Other project"),
        NOW,
    )
    assert result.outcome is DecisionOutcome.ENQUEUED
