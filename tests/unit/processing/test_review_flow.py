"""The model may hold an action; only a person can release it."""

import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from processing_support import Gateway, Source
from support import AS_OF, repo_policy
from techcom_followup.adapters.outbound.clock import FixedClock
from techcom_followup.adapters.outbound.in_memory_repository import (
    InMemoryRepository,
)
from techcom_followup.adapters.outbound.llm_stub import ScriptedLLM
from techcom_followup.adapters.outbound.run_lock import InMemoryRunLock
from techcom_followup.application.dispatch import (
    DeliveryDependencies,
    DispatchActions,
)
from techcom_followup.application.orchestrator import FollowUpOrchestrator
from techcom_followup.application.process_tasks import (
    ProcessingDependencies,
    ProcessTasks,
)
from techcom_followup.application.repository import ActionStatus
from techcom_followup.application.review import ReviewHeld
from techcom_followup.domain.models import Flag
from techcom_followup.infrastructure.telemetry import PrometheusTelemetry

RECORD = {
    "task_id": "TC-4001",
    "project": "SURF Illinois",
    "task": "Fiber splicing",
    "assigned_to": "John Smith",
    "due_date": "2026-10-18",
    "status": "Working on it",
    "last_update": "2026-10-16",
    "notes": "Crew says the trailer is fine but the splice is on hold.",
}
HOLD = {
    "tool": "submit_plan",
    "plan": {
        "decision": "follow_up_owner",
        "recipient": "owner",
        "priority": "normal",
        "asks": ["status", "blocker"],
        "tone": "neutral",
        "earlier_contact": False,
        "delivery": "hold_for_review",
        "hold_reason": "conflicting_signals",
        "rationale": "The note says fine and on hold at the same time.",
        "evidence": ["the splice is on hold"],
    },
}


@pytest.mark.owasp_llm("LLM06")
async def test_held_action_waits_until_a_person_approves_it() -> None:
    repository = InMemoryRepository()
    clock = FixedClock(datetime(2026, 10, 21, tzinfo=UTC))
    policy = repo_policy()
    telemetry = PrometheusTelemetry()
    process = ProcessTasks(
        ProcessingDependencies(
            repository, Source([RECORD]), policy, clock, InMemoryRunLock()
        ),
        planner=FollowUpOrchestrator(
            ScriptedLLM([json.dumps(HOLD)]),
            repository,
            policy,
            telemetry=telemetry,
        ),
    )
    gateway = Gateway()
    dispatch = DispatchActions(
        DeliveryDependencies(repository, gateway, clock)
    )
    report = await process.execute(as_of=AS_OF)
    assert report.enqueued == 1
    (action,) = await repository.list_actions()
    assert action.status is ActionStatus.HELD
    assert Flag.HELD_FOR_REVIEW in action.decision.flags
    assert "Is anything blocking it" in action.message.body
    assert await dispatch.execute() == 0
    assert gateway.sends == []
    reviewer = ReviewHeld(repository, clock, telemetry)
    assert [item.id for item in await reviewer.held()] == [action.id]
    await reviewer.decide(action.identity.operation_id, approve=True)
    assert await reviewer.held() == ()
    assert await dispatch.execute() == 1
    assert gateway.sends == [action.identity.operation_id]
    rerun = await process.execute(as_of=AS_OF)
    assert rerun.duplicates == 1
    exported = telemetry.registry.get_sample_value(
        "followup_agent_plans_total", {"outcome": "held"}
    )
    assert exported == 1
    assert (
        telemetry.registry.get_sample_value(
            "followup_actions_total", {"outcome": "approved"}
        )
        == 1
    )


async def test_unknown_operation_cannot_be_reviewed() -> None:
    clock = FixedClock(datetime(2026, 10, 21, tzinfo=UTC))
    reviewer = ReviewHeld(InMemoryRepository(), clock)
    assert await reviewer.held() == ()
    assert await reviewer.decide(uuid4(), approve=False) is None
