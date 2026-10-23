"""Planned actions keep the policy's dedupe slot and audit trail."""

import itertools
from dataclasses import replace
from datetime import date
from uuid import uuid4

import pytest

from persistence_support import NOW
from processing_support import Source, fixture_records
from support import AS_OF, make_task, repo_policy
from techcom_followup.adapters.outbound.clock import FixedClock
from techcom_followup.adapters.outbound.in_memory_repository import (
    InMemoryRepository,
)
from techcom_followup.adapters.outbound.llm_agent_stub import (
    OfflineLanguageModel,
)
from techcom_followup.adapters.outbound.run_lock import InMemoryRunLock
from techcom_followup.application.orchestrator import FollowUpOrchestrator
from techcom_followup.application.process_tasks import (
    ProcessingDependencies,
    ProcessTasks,
)
from techcom_followup.application.processing_ports import PlannedAction
from techcom_followup.application.repository import DecisionWrite
from techcom_followup.domain.identity import ActionIdentity, state_fingerprint
from techcom_followup.domain.messages import compose
from techcom_followup.domain.models import (
    Decision,
    DecisionKind,
    Flag,
    Target,
    TargetKind,
    Task,
    TaskId,
)
from techcom_followup.domain.policy import RoutingTable

POLICY = repo_policy()


def processor(repository: InMemoryRepository, planner: object) -> ProcessTasks:
    return ProcessTasks(
        ProcessingDependencies(
            repository,
            Source(fixture_records()),
            POLICY,
            FixedClock(NOW),
            InMemoryRunLock(),
        ),
        planner=planner,  # type: ignore[arg-type]
    )


class FlipFlopPlanner:
    """A nondeterministic model: every call picks another recipient."""

    def __init__(self, routing: RoutingTable) -> None:
        self.calls = 0
        self._routing = routing
        self._names = itertools.cycle(("Alpha desk", "Beta desk"))

    async def plan(
        self, decision: Decision, task: Task, *, as_of: date
    ) -> PlannedAction:
        self.calls += 1
        target = Target(TargetKind.TEAM, next(self._names), task.project)
        planned = replace(decision, target=target)
        return PlannedAction(
            planned, compose(planned, task, as_of), {"outcome": "flip"}
        )


async def test_rerun_never_calls_the_model_or_sends_twice() -> None:
    repository = InMemoryRepository()
    model = OfflineLanguageModel()
    planner = FollowUpOrchestrator(model, repository, POLICY)
    first = await processor(repository, planner).execute(as_of=AS_OF)
    calls = model.calls
    second = await processor(repository, planner).execute(as_of=AS_OF)
    assert first.enqueued == 7
    assert second.enqueued == 0
    assert second.duplicates == 7
    assert model.calls == calls
    plans = [
        event.payload["plan"]
        for event in await repository.list_audit()
        if event.kind == "decision.enqueued"
    ]
    assert len(plans) == 7
    assert all(
        isinstance(plan, dict) and plan["outcome"] == "accepted"
        for plan in plans
    )
    planned = [d for d in first.decisions if Flag.AGENT_PLANNED in d.flags]
    assert len(planned) == 7


async def test_model_variance_cannot_create_a_second_action() -> None:
    repository = InMemoryRepository()
    planner = FlipFlopPlanner(POLICY.routing)
    await processor(repository, planner).execute(as_of=AS_OF)
    # Forget nothing but call the planner again on a fresh repository
    # view of the same slots: identity comes from the baseline.
    first = {a.identity for a in await repository.list_actions()}
    again = await processor(repository, planner).execute(as_of=AS_OF)
    assert again.enqueued == 0
    assert {a.identity for a in await repository.list_actions()} == first
    assert planner.calls == 7


def test_planned_writes_must_refine_an_actionable_baseline() -> None:
    task = make_task(due_date=date(2026, 10, 18))
    baseline = POLICY.decide(task, as_of=AS_OF)
    fingerprint = state_fingerprint(task)
    identity = ActionIdentity.of(baseline, fingerprint)
    message = compose(baseline, task, AS_OF)
    escalated = replace(baseline, kind=DecisionKind.ESCALATE)
    run = uuid4()
    DecisionWrite(
        run, escalated, fingerprint, identity, message, baseline=baseline
    ).validate()
    with pytest.raises(ValueError, match="identity does not match"):
        DecisionWrite(
            run, escalated, fingerprint, identity, message
        ).validate()
    other = replace(baseline, task_id=TaskId("TC-other"))
    with pytest.raises(ValueError, match="refine an actionable baseline"):
        DecisionWrite(
            run,
            escalated,
            fingerprint,
            ActionIdentity.of(other, fingerprint),
            message,
            baseline=other,
        ).validate()
    quiet = replace(baseline, kind=DecisionKind.NO_ACTION, target=None)
    with pytest.raises(ValueError, match="refine an actionable baseline"):
        DecisionWrite(run, quiet, fingerprint, baseline=baseline).validate()


async def test_lossy_project_names_are_followed_up_without_authority() -> None:
    records = [
        record | {"project": record["project"] + " "}
        for record in fixture_records()
        if isinstance(record, dict)
    ]
    repository = InMemoryRepository()
    report = await ProcessTasks(
        ProcessingDependencies(
            repository,
            Source(list(records)),
            POLICY,
            FixedClock(NOW),
            InMemoryRunLock(),
        )
    ).execute(as_of=AS_OF)
    assert report.rejected == ()
    assert report.enqueued == 7
    actions = await repository.list_actions()
    assert {action.source_project for action in actions} == {None}
