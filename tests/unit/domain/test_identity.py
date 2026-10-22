import uuid
from datetime import date

import pytest

from support import AS_OF, make_task
from techcom_followup.domain.identity import (
    NAMESPACE_FOLLOWUP,
    ActionIdentity,
    canonical_json,
    state_fingerprint,
)
from techcom_followup.domain.models import (
    Decision,
    DecisionKind,
    ReasonCode,
    Target,
    TargetKind,
    TaskId,
    TaskStatus,
)
from techcom_followup.domain.policy import DecisionPolicy

OWNER = Target(TargetKind.PERSON, "John Smith", "SURF Illinois")


def follow_up(target: Target | None = OWNER) -> Decision:
    return Decision(
        task_id=TaskId("TC-1"),
        kind=DecisionKind.FOLLOW_UP_OWNER,
        target=target,
        rule_id="R7",
        reason_code=ReasonCode.OVERDUE_NO_EXPLANATION,
        reason="1 day(s) overdue; no blocker explained.",
    )


def test_namespace_is_the_documented_uuid5() -> None:
    expected = uuid.uuid5(uuid.NAMESPACE_URL, "urn:techcom:followup:operation")
    assert expected == NAMESPACE_FOLLOWUP


def test_canonical_json_is_order_independent_and_compact() -> None:
    assert canonical_json({"b": 1, "a": {"y": None, "x": "é"}}) == (
        '{"a":{"x":"é","y":null},"b":1}'
    )


def test_fingerprint_ignores_case_spacing_and_last_update() -> None:
    base = make_task(notes="Waiting on permit")
    noisy = make_task(
        owner="john  SMITH",
        notes="  waiting   ON permit ",
        last_update=date(2026, 1, 1),
    )
    assert state_fingerprint(base) == state_fingerprint(noisy)


@pytest.mark.parametrize(
    "change",
    [
        {"status": TaskStatus.BLOCKED},
        {"due_date": date(2026, 10, 31)},
        {"due_date": None},
        {"owner": "Maria Cruz"},
        {"owner": None},
        {"notes": "Permit approved"},
    ],
)
def test_fingerprint_changes_with_decision_relevant_fields(
    change: dict[str, object],
) -> None:
    assert state_fingerprint(make_task()) != state_fingerprint(
        make_task(**change)
    )


def test_identity_is_stable_and_derived() -> None:
    first = ActionIdentity.of(follow_up(), "abc")
    second = ActionIdentity.of(follow_up(), "abc")
    assert first == second
    assert first.idempotency_key.startswith("sha256:")
    assert len(first.idempotency_key) == len("sha256:") + 64
    assert first.operation_id == uuid.uuid5(
        NAMESPACE_FOLLOWUP, first.idempotency_key
    )
    assert first.operation_id.version == 5


def test_identity_ignores_recipient_case() -> None:
    shouting = Target(TargetKind.PERSON, "JOHN SMITH", "surf illinois")
    assert ActionIdentity.of(follow_up(), "fp") == ActionIdentity.of(
        follow_up(shouting), "fp"
    )


@pytest.mark.parametrize(
    "other",
    [
        follow_up(Target(TargetKind.PERSON, "Maria Cruz", "SURF Illinois")),
        follow_up(Target(TargetKind.TEAM, "John Smith", "SURF Illinois")),
        follow_up(Target(TargetKind.PERSON, "John Smith", None)),
    ],
)
def test_identity_changes_with_the_recipient(other: Decision) -> None:
    assert ActionIdentity.of(follow_up(), "fp") != ActionIdentity.of(
        other, "fp"
    )


def test_identity_changes_with_state() -> None:
    assert ActionIdentity.of(follow_up(), "a") != ActionIdentity.of(
        follow_up(), "b"
    )


def test_no_action_has_no_identity(policy: DecisionPolicy) -> None:
    decision = policy.decide(make_task(), as_of=AS_OF)
    with pytest.raises(ValueError, match="no outbound action"):
        ActionIdentity.of(decision, "fp")
