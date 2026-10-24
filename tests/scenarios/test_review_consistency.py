from dataclasses import replace

import pytest

from processing_support import Source, fixture_records, stack
from support import AS_OF, repo_policy
from techcom_followup.adapters.outbound.llm_agent_stub import (
    OfflineLanguageModel,
)
from techcom_followup.application.orchestrator import FollowUpOrchestrator
from techcom_followup.application.repository import (
    ActionStatus,
    DecisionOutcome,
)
from techcom_followup.domain.models import DecisionKind


async def test_duplicate_report_reuses_the_whole_escalation_plan() -> None:
    record = fixture_records()[0]
    assert isinstance(record, dict)
    source = Source([{**record, "notes": "first snapshot"}])
    async with stack() as app:
        app.processor._deps = replace(app.processor._deps, source=source)
        app.processor._planner = FollowUpOrchestrator(
            OfflineLanguageModel(), app.repository, repo_policy()
        )
        await app.processor.execute(as_of=AS_OF)
        await app.dispatcher.execute()
        source.records = [{**record, "notes": "second snapshot"}]
        planned = await app.processor.execute(as_of=AS_OF)
        rerun = await app.processor.execute(as_of=AS_OF)
        assert planned.decisions[0].kind is DecisionKind.ESCALATE
        assert rerun.decisions == planned.decisions
        assert rerun.plans[0].message == planned.plans[0].message
        assert rerun.plans[0].plan_audit == planned.plans[0].plan_audit
        assert rerun.duplicates == 1
        assert rerun.enqueued == 0


async def test_source_regression_after_done_never_reaches_delivery() -> None:
    record = fixture_records()[0]
    assert isinstance(record, dict)
    source = Source(
        [{**record, "status": "Done", "last_update": "2026-10-21T15:00:00Z"}]
    )
    async with stack() as app:
        app.processor._deps = replace(app.processor._deps, source=source)
        await app.processor.execute(as_of=AS_OF)
        source.records = [{**record, "last_update": "2026-10-20T09:00:00Z"}]
        stale = await app.processor.execute(as_of=AS_OF)
        assert stale.run_id is not None
        (receipt,) = await app.repository.list_decisions(stale.run_id)
        assert receipt.outcome is DecisionOutcome.STALE_SUPPRESSED
        assert stale.enqueued == 0
        assert await app.dispatcher.execute() == 0
        source.records = [{**record, "last_update": "2026-10-22T09:00:00Z"}]
        fresh = await app.processor.execute(as_of=AS_OF.replace(day=22))
        assert fresh.enqueued == 1
        assert await app.dispatcher.execute() == 1


@pytest.mark.parametrize("project", [" SURF Illinois", "SURF ILLINOIS"])
async def test_reused_identity_does_not_reuse_foreign_project_authority(
    project: str,
) -> None:
    record = fixture_records()[0]
    assert isinstance(record, dict)
    source = Source([record])
    async with stack() as app:
        app.processor._deps = replace(app.processor._deps, source=source)
        await app.processor.execute(as_of=AS_OF)
        source.records = [{**record, "project": project}]
        rerun = await app.processor.execute(as_of=AS_OF)
        assert rerun.enqueued == 0
        expected = None if project.startswith(" ") else project
        assert rerun.plans[0].source_project == expected
        target = rerun.decisions[0].target
        assert target is not None
        assert target.project == project.strip()


async def test_future_dated_snapshot_cannot_fence_later_updates() -> None:
    record = fixture_records()[0]
    assert isinstance(record, dict)
    source = Source(
        [{**record, "status": "Blocked", "last_update": "2099-01-01"}]
    )
    async with stack() as app:
        app.processor._deps = replace(app.processor._deps, source=source)
        assert (await app.processor.execute(as_of=AS_OF)).enqueued == 1
        source.records = [
            {**record, "status": "Done", "last_update": "2026-10-21T15:00:00Z"}
        ]
        done = await app.processor.execute(as_of=AS_OF)
        assert done.run_id is not None
        (receipt,) = await app.repository.list_decisions(done.run_id)
        assert receipt.outcome is DecisionOutcome.NONE
        (action,) = await app.repository.list_actions()
        assert action.status is ActionStatus.DEAD
        assert await app.dispatcher.execute() == 0


async def test_done_task_without_project_retires_its_reminder() -> None:
    record = fixture_records()[0]
    assert isinstance(record, dict)
    base = {key: value for key, value in record.items() if key != "project"}
    source = Source([{**base, "status": "Blocked"}])
    async with stack() as app:
        app.processor._deps = replace(app.processor._deps, source=source)
        assert (await app.processor.execute(as_of=AS_OF)).enqueued == 1
        source.records = [{**base, "status": "Done"}]
        await app.processor.execute(as_of=AS_OF)
        (action,) = await app.repository.list_actions()
        assert action.status is ActionStatus.DEAD
        assert action.last_error == "source_state_superseded"
        assert await app.dispatcher.execute() == 0
