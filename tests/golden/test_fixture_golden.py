"""Golden test: the brief's fixture end to end (``docs/00`` s. 6).

The explicit table below is what a reviewer reads; the snapshot pins
everything else (reasons, messages, keys) so that any behaviour change
shows up as a reviewable diff. Update it with
``uv run pytest tests/golden --snapshot-update``.
"""

from datetime import date
from typing import Any

import pytest
from syrupy.assertion import SnapshotAssertion
from syrupy.extensions.json import JSONSnapshotExtension

from support import assess
from techcom_followup.domain.identity import ActionIdentity, state_fingerprint
from techcom_followup.domain.messages import compose
from techcom_followup.domain.models import Task
from techcom_followup.domain.normalize import normalize
from techcom_followup.domain.policy import DecisionPolicy

EXPECTED = {
    # Per task: decision, rule, recipient, priority and flags.
    "TC-1001": (
        "follow_up_owner",
        "R7",
        "John Smith",
        "normal",
        ["llm_fallback"],
    ),
    "TC-1002": ("request_eta", "R5", "Warehouse team", "high", []),
    "TC-1003": ("no_action", "R1", None, "normal", []),
    "TC-1004": (
        "clarify_missing_info",
        "R3",
        "Wisconsin Build coordinator",
        "normal",
        [],
    ),
    "TC-1005": ("clarify_missing_info", "R3", "Alex Brown", "normal", []),
    "TC-1006": (
        "follow_up_owner",
        "R7",
        "Jamie Lee",
        "normal",
        ["prompt_injection_suspected"],
    ),
    "TC-1007": ("no_action", "R9", None, "normal", []),
    "TC-1008": (
        "escalate",
        "R6",
        "SURF Illinois project manager",
        "high",
        [],
    ),
    "TC-1009": ("request_eta", "R5", "Warehouse team", "high", []),
    "TC-1010": ("no_action", "R1", None, "normal", []),
}


@pytest.fixture
def snapshot_json(snapshot: SnapshotAssertion) -> SnapshotAssertion:
    return snapshot.use_extension(JSONSnapshotExtension)


def fixture_tasks(records: list[object], as_of: date) -> tuple[Task, ...]:
    result = normalize(records, as_of)
    assert result.rejected == ()
    return result.tasks


def describe(
    task: Task, policy: DecisionPolicy, as_of: date
) -> dict[str, Any]:
    assessment = assess(policy, task, as_of)
    decision = assessment.decision
    row: dict[str, Any] = {
        "task_id": decision.task_id,
        "decision": decision.kind.value,
        "rule_id": decision.rule_id,
        "reason_code": decision.reason_code.value,
        "reason": decision.reason,
        "priority": decision.priority.value,
        "flags": sorted(flag.value for flag in decision.flags),
        "llm_needed": assessment.llm_needed,
        "target": None,
        "evidence": None,
        "message": None,
        "idempotency_key": None,
        "operation_id": None,
    }
    if decision.interpretation is not None:
        row["evidence"] = {
            "source": decision.interpretation.source.value,
            "blocker": decision.interpretation.blocker.value,
            "quote": decision.interpretation.evidence,
        }
    if decision.target is not None:
        draft = compose(decision, task, as_of)
        identity = ActionIdentity.of(decision, state_fingerprint(task))
        row |= {
            "target": {
                "kind": decision.target.kind.value,
                "name": decision.target.name,
                "project": decision.target.project,
            },
            "message": {
                "subject": draft.subject,
                "body": draft.body,
                "template_id": draft.template_id,
                "source": draft.source.value,
            },
            "idempotency_key": identity.idempotency_key,
            "operation_id": str(identity.operation_id),
        }
    return row


def test_fixture_decisions_match_the_analysis(
    fixture_records: list[object],
    policy: DecisionPolicy,
    as_of: date,
) -> None:
    rows = [
        describe(task, policy, as_of)
        for task in fixture_tasks(fixture_records, as_of)
    ]
    actual = {
        row["task_id"]: (
            row["decision"],
            row["rule_id"],
            row["target"] and row["target"]["name"],
            row["priority"],
            row["flags"],
        )
        for row in rows
    }
    assert actual == EXPECTED


def test_fixture_snapshot(
    fixture_records: list[object],
    policy: DecisionPolicy,
    as_of: date,
    snapshot_json: SnapshotAssertion,
) -> None:
    rows = [
        describe(task, policy, as_of)
        for task in fixture_tasks(fixture_records, as_of)
    ]
    assert rows == snapshot_json


def test_only_tc_1001_would_reach_the_llm(
    fixture_records: list[object],
    policy: DecisionPolicy,
    as_of: date,
) -> None:
    needed = [
        task.task_id
        for task in fixture_tasks(fixture_records, as_of)
        if assess(policy, task, as_of).llm_needed
    ]
    assert needed == ["TC-1001"]


def test_injection_note_is_not_echoed(
    fixture_records: list[object],
    policy: DecisionPolicy,
    as_of: date,
) -> None:
    tasks: dict[str, Task] = {
        t.task_id: t for t in fixture_tasks(fixture_records, as_of)
    }
    row = describe(tasks["TC-1006"], policy, as_of)
    assert row["evidence"] is None
    assert "Ignore" not in row["message"]["body"]
    assert "complete" not in row["message"]["body"].lower().replace(
        "completion", ""
    )


def test_materials_message_asks_for_delivery_not_completion(
    fixture_records: list[object],
    policy: DecisionPolicy,
    as_of: date,
) -> None:
    tasks: dict[str, Task] = {
        t.task_id: t for t in fixture_tasks(fixture_records, as_of)
    }
    body = describe(tasks["TC-1002"], policy, as_of)["message"]["body"]
    assert "delivery date" in body
    assert "complete" not in body.lower()
