from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from persistence_support import NOW, action_write
from support import AS_OF
from techcom_followup.application.repository import (
    ActionStatus,
    ClaimRequest,
    DecisionOutcome,
    DecisionWrite,
    Repository,
)
from techcom_followup.application.retention import PruneData, RetentionPolicy
from techcom_followup.domain.identity import ActionIdentity
from techcom_followup.domain.models import DecisionKind, Flag
from techcom_followup.domain.source_version import SourceVersion


def version(day: int, hour: int = 0, *, coarse: bool = False) -> SourceVersion:
    return SourceVersion(datetime(2026, 10, day, hour, tzinfo=UTC), coarse)


async def next_write(
    repository: Repository,
    write: DecisionWrite,
    source: SourceVersion | None,
    fingerprint: str,
) -> DecisionWrite:
    run = await repository.start_run(AS_OF, "version-test", NOW)
    return replace(
        write,
        run_id=run.id,
        source_version=source,
        fingerprint=fingerprint,
        identity=ActionIdentity.of(write.decision, fingerprint),
    )


@pytest.mark.parametrize("held", [False, True])
async def test_stale_snapshot_cannot_cancel_current_work(
    repository: Repository, held: bool
) -> None:
    write = replace(
        await action_write(repository), source_version=version(21, 15)
    )
    if held:
        write = replace(
            write,
            decision=replace(
                write.decision, flags=frozenset({Flag.HELD_FOR_REVIEW})
            ),
        )
    await repository.record_decision(write, NOW)
    stale = await next_write(repository, write, version(21, 9), "older-state")
    result = await repository.record_decision(stale, NOW)
    assert result.outcome is DecisionOutcome.STALE_SUPPRESSED
    (action,) = await repository.list_actions()
    assert action.status is (
        ActionStatus.HELD if held else ActionStatus.PENDING
    )
    assert (await repository.list_audit())[
        -1
    ].kind == "decision.stale_suppressed"


async def test_done_watermark_blocks_old_state_but_allows_real_reopening(
    repository: Repository,
) -> None:
    write = await action_write(repository)
    quiet = replace(
        write,
        decision=replace(
            write.decision, kind=DecisionKind.NO_ACTION, target=None
        ),
        identity=None,
        message=None,
        fingerprint="completed-state",
        source_version=version(21, 15),
    )
    await repository.record_decision(quiet, NOW)
    stale = await next_write(repository, write, version(20), "old-state")
    assert (
        await repository.record_decision(stale, NOW)
    ).outcome is DecisionOutcome.STALE_SUPPRESSED
    assert await repository.list_actions() == ()
    fresh = await next_write(repository, write, version(22), "reopened-state")
    assert (
        await repository.record_decision(fresh, NOW)
    ).outcome is DecisionOutcome.ENQUEUED
    assert len(await repository.claim_actions(ClaimRequest(NOW))) == 1


@pytest.mark.parametrize(
    "source",
    [version(21, 15), version(21, coarse=True), None],
)
async def test_equal_coarse_and_missing_versions_have_explicit_semantics(
    repository: Repository, source: SourceVersion | None
) -> None:
    write = replace(
        await action_write(repository), source_version=version(21, 15)
    )
    await repository.record_decision(write, NOW)
    # Missing versions may replay unchanged state, not replace facts.
    fingerprint = write.fingerprint if source is None else "observed-change"
    incoming = await next_write(repository, write, source, fingerprint)
    result = await repository.record_decision(incoming, NOW)
    assert result.outcome is (
        DecisionOutcome.DUPLICATE_SUPPRESSED
        if source is None
        else DecisionOutcome.ENQUEUED
    )
    stale = await next_write(repository, write, version(21, 9), "older-state")
    assert (
        await repository.record_decision(stale, NOW)
    ).outcome is DecisionOutcome.STALE_SUPPRESSED
    missing = await next_write(repository, write, None, "unversioned-change")
    assert (
        await repository.record_decision(missing, NOW)
    ).outcome is DecisionOutcome.STALE_SUPPRESSED


async def test_replay_keeps_watermark_and_projects_are_isolated(
    repository: Repository,
) -> None:
    write = replace(await action_write(repository), source_version=version(20))
    saved = await repository.record_decision(write, NOW)
    assert (
        await repository.record_decision(
            replace(write, source_version=version(23)), NOW
        )
        == saved
    )
    fresh = await next_write(repository, write, version(21), "new-state")
    assert (
        await repository.record_decision(fresh, NOW)
    ).outcome is DecisionOutcome.ENQUEUED
    foreign = await next_write(repository, write, version(19), "other-project")
    target = foreign.decision.target
    assert target is not None
    foreign = replace(
        foreign,
        source_project="Another project",
        decision=replace(
            foreign.decision, target=replace(target, project="Another project")
        ),
    )
    foreign = replace(
        foreign,
        identity=ActionIdentity.of(foreign.decision, foreign.fingerprint),
    )
    assert (
        await repository.record_decision(foreign, NOW)
    ).outcome is DecisionOutcome.ENQUEUED


async def test_quiet_watermark_outlives_pruned_history(
    repository: Repository,
) -> None:
    write = await action_write(repository)
    quiet = replace(
        write,
        decision=replace(
            write.decision, kind=DecisionKind.NO_ACTION, target=None
        ),
        identity=None,
        message=None,
        source_version=version(21),
    )
    await repository.record_decision(quiet, NOW)
    await repository.finish_run(quiet.run_id, {"status": "completed"}, NOW)
    await PruneData(
        repository,
        RetentionPolicy(
            version="2026-10-24.1",
            decision_days=90,
            action_days=365,
            audit_days=365,
            batch_size=100,
            max_batches=100,
        ),
        lambda: NOW + timedelta(days=400),
    ).execute()
    assert await repository.get_run(quiet.run_id) is None
    stale = await next_write(repository, write, version(20), "obsolete")
    assert (
        await repository.record_decision(stale, NOW)
    ).outcome is DecisionOutcome.STALE_SUPPRESSED
