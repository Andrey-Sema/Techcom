from dataclasses import replace

from processing_support import Source, fixture_records, stack
from support import AS_OF, repo_policy
from techcom_followup.adapters.outbound.llm_agent_stub import (
    OfflineLanguageModel,
)
from techcom_followup.application.orchestrator import FollowUpOrchestrator
from techcom_followup.application.repository import DecisionOutcome
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
