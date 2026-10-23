"""The policy's envelope bounds every choice an LLM planner can make."""

from datetime import date

import pytest

from support import AS_OF, make_task, repo_policy
from techcom_followup.domain.envelope import (
    ASSIGNED_TEAM,
    ActionEnvelope,
    build_envelope,
    priority_rank,
)
from techcom_followup.domain.models import (
    BlockerCategory,
    DecisionKind,
    InterpretationSource,
    NoteInterpretation,
    Priority,
    TargetKind,
    Task,
    TaskStatus,
)

POLICY = repo_policy()
MATERIALS = NoteInterpretation(
    BlockerCategory.MATERIALS,
    needs_eta=True,
    confidence=0.95,
    evidence="materials",
    source=InterpretationSource.KEYWORD,
)


def envelope_for(
    task: Task,
    *,
    chased: int = 0,
    interpretation: NoteInterpretation | None = None,
) -> ActionEnvelope | None:
    decision = POLICY.decide(task, as_of=AS_OF, interpretation=interpretation)
    return build_envelope(
        decision,
        task,
        POLICY.routing,
        as_of=AS_OF,
        prior_follow_ups=chased,
    )


def test_quiet_decisions_have_no_envelope() -> None:
    assert envelope_for(make_task(status=TaskStatus.DONE)) is None
    assert envelope_for(make_task()) is None


def test_first_overdue_follow_up_cannot_escalate() -> None:
    envelope = envelope_for(make_task(due_date=date(2026, 10, 18)))
    assert envelope is not None
    assert dict(envelope.options) == {
        DecisionKind.FOLLOW_UP_OWNER: frozenset({"owner"})
    }
    assert DecisionKind.NO_ACTION not in envelope.options
    assert envelope.describe()["baseline"] == {
        "decision": "follow_up_owner",
        "recipient": "owner",
        "priority": "normal",
    }


def test_unanswered_overdue_follow_up_may_go_to_the_manager() -> None:
    envelope = envelope_for(make_task(due_date=date(2026, 10, 18)), chased=1)
    assert envelope is not None
    assert envelope.permits(DecisionKind.ESCALATE, "manager")
    assert not envelope.permits(DecisionKind.ESCALATE, "owner")
    assert envelope.target("manager").name == "SURF Illinois project manager"


def test_chased_task_that_is_not_overdue_stays_with_its_owner() -> None:
    envelope = envelope_for(
        make_task(due_date=AS_OF, status=TaskStatus.NOT_STARTED), chased=2
    )
    assert envelope is not None
    assert not envelope.permits_kind(DecisionKind.ESCALATE)


def test_materials_request_may_ask_the_owner_instead_of_the_team() -> None:
    envelope = envelope_for(
        make_task(status=TaskStatus.BLOCKED), interpretation=MATERIALS
    )
    assert envelope is not None
    assert envelope.options[DecisionKind.REQUEST_ETA] == frozenset(
        {ASSIGNED_TEAM, "owner"}
    )
    assert envelope.recipients[ASSIGNED_TEAM].kind is TargetKind.TEAM
    describe = envelope.describe()
    assert describe["recipients"] == {
        ASSIGNED_TEAM: "team",
        "coordinator": "role",
        "manager": "role",
        "owner": "person",
    }
    # Personal names never reach the model through the envelope.
    assert "John Smith" not in str(describe)


def test_materials_request_without_owner_has_no_owner_option() -> None:
    task = make_task(status=TaskStatus.BLOCKED, owner=None, due_date=AS_OF)
    decision = POLICY.decide(task, as_of=AS_OF, interpretation=MATERIALS)
    assert decision.kind is DecisionKind.CLARIFY_MISSING_INFO
    envelope = envelope_for(task, interpretation=MATERIALS)
    assert envelope is not None
    assert envelope.options == {
        DecisionKind.CLARIFY_MISSING_INFO: frozenset({"coordinator"})
    }


def test_clarification_may_also_go_to_the_coordinator() -> None:
    envelope = envelope_for(make_task(due_date=None))
    assert envelope is not None
    assert envelope.options == {
        DecisionKind.CLARIFY_MISSING_INFO: frozenset({"owner", "coordinator"})
    }


def test_escalation_can_change_recipient_but_never_be_downgraded() -> None:
    envelope = envelope_for(make_task(due_date=date(2026, 10, 1)), chased=3)
    assert envelope is not None
    assert envelope.options == {
        DecisionKind.ESCALATE: frozenset({"manager", "coordinator"})
    }
    assert envelope.min_priority is Priority.HIGH


def test_unknown_project_routes_to_the_fallback_team() -> None:
    envelope = envelope_for(make_task(project="Unlisted", owner=None))
    assert envelope is not None
    assert envelope.target("coordinator").name == "Operations desk"


@pytest.mark.parametrize(("low", "high"), [(Priority.NORMAL, Priority.HIGH)])
def test_priorities_are_ordered(low: Priority, high: Priority) -> None:
    assert priority_rank(low) < priority_rank(high)
