"""Planned decisions keep their baseline slot and plan trace."""

from dataclasses import replace

from persistence_support import NOW, action_write
from support import AS_OF
from techcom_followup.application.repository import (
    DecisionOutcome,
    Repository,
)
from techcom_followup.domain.models import DecisionKind, Flag


async def test_planned_decision_round_trips_with_its_trace(
    repository: Repository,
) -> None:
    baseline = await action_write(repository)
    planned = replace(
        baseline,
        decision=replace(
            baseline.decision,
            kind=DecisionKind.ESCALATE,
            flags=frozenset({Flag.AGENT_PLANNED}),
            rationale="An earlier follow-up went unanswered.",
        ),
        baseline=baseline.decision,
        plan_audit={"outcome": "accepted", "tools": ["submit_plan"]},
    )
    stored = await repository.record_decision(planned, NOW)
    assert stored.outcome is DecisionOutcome.ENQUEUED
    (read,) = await repository.list_decisions(planned.run_id)
    assert read.write == planned
    (action,) = await repository.list_actions()
    assert action.identity == baseline.identity
    assert action.decision.kind is DecisionKind.ESCALATE
    (event,) = [
        item
        for item in await repository.list_audit()
        if item.kind == "decision.enqueued"
    ]
    assert event.payload["plan"] == {
        "outcome": "accepted",
        "tools": ["submit_plan"],
    }
    # The same task state planned differently is still one slot.
    rerun = await repository.start_run(AS_OF, "v", NOW)
    again = replace(baseline, run_id=rerun.id, baseline=None, plan_audit=None)
    duplicate = await repository.record_decision(again, NOW)
    assert duplicate.outcome is DecisionOutcome.DUPLICATE_SUPPRESSED
