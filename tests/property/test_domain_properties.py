"""Property-based invariants of the domain core (``docs/06`` s. 3).

Numbers in test names refer to the invariant list in that section.
"""

import re
from datetime import date, timedelta
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from support import AS_OF, repo_policy
from techcom_followup.domain.identity import state_fingerprint
from techcom_followup.domain.messages import (
    BODY_MAX_CHARS,
    BODY_MIN_CHARS,
    SUBJECT_MAX_CHARS,
    compose,
)
from techcom_followup.domain.models import (
    BlockerCategory,
    DecisionKind,
    InterpretationSource,
    NoteInterpretation,
    RejectedRecord,
    ScreenResult,
    TargetKind,
    Task,
    TaskStatus,
)
from techcom_followup.domain.normalize import normalize

POLICY = repo_policy()
ROUTED_NAMES = frozenset(
    {"Operations desk", "Warehouse team"}
    | {
        f"{project} {role}"
        for project in (
            "SURF Illinois",
            "Michigan Expansion",
            "Wisconsin Build",
            "Indiana OSP",
            "Minnesota Fiber",
        )
        for role in ("coordinator", "project manager")
    }
)
DECIDED_EARLY = frozenset({"R1", "R3", "R4"})

dates = st.dates(AS_OF - timedelta(days=40), AS_OF + timedelta(days=40))
statuses = st.sampled_from(
    ["Working on it", "Blocked", "Done", "Not Started", "stuck", "??"]
)
records = st.fixed_dictionaries(
    {
        "task_id": st.just("TC-1"),
        "project": st.sampled_from(["SURF Illinois", "Nowhere", ""]),
        "task": st.text(max_size=120),
        "assigned_to": st.none() | st.sampled_from(["John Smith", "n/a"]),
        "due_date": st.none() | dates.map(date.isoformat),
        "status": statuses,
        "last_update": st.none() | dates.map(date.isoformat),
        "notes": st.text(max_size=300),
    }
)
interpretations = st.none() | st.builds(
    NoteInterpretation,
    blocker=st.sampled_from(BlockerCategory),
    needs_eta=st.booleans(),
    confidence=st.floats(0.0, 1.0),
    evidence=st.text(max_size=20),
    source=st.sampled_from(InterpretationSource),
)
screens = st.builds(
    ScreenResult, suspicious=st.booleans(), matched_patterns=st.just(())
)
json_values = st.recursive(
    st.none()
    | st.booleans()
    | st.integers()
    | st.floats(allow_nan=True)
    | st.text(),
    lambda inner: st.lists(inner) | st.dictionaries(st.text(), inner),
    max_leaves=10,
)


def to_task(record: dict[str, Any]) -> Task:
    (task,) = normalize([record], AS_OF).tasks
    return task


@settings(max_examples=300)
@given(records, interpretations, screens)
def test_1_2_3_4_5_decisions_are_total_and_safe(
    record: dict[str, Any],
    interpretation: NoteInterpretation | None,
    screen: ScreenResult,
) -> None:
    task = to_task(record)
    decision = POLICY.decide(
        task, as_of=AS_OF, screen=screen, interpretation=interpretation
    )
    # 1. Totality: exactly one decision, for this task.
    assert decision.task_id == task.task_id
    # 2. Done is quiet.
    if task.status is TaskStatus.DONE:
        assert decision.kind is DecisionKind.NO_ACTION
        return
    # 3. No invention: recipients are the owner or configured roles.
    if decision.target is not None:
        if decision.target.kind is TargetKind.PERSON:
            assert decision.target.name == task.owner
        else:
            assert decision.target.name in ROUTED_NAMES
    # 4. Missing information is never guessed.
    if task.owner is None or task.due_date is None:
        assert decision.kind is DecisionKind.CLARIFY_MISSING_INFO
        return
    # 5. Overdue is never silenced, whatever the note or the model says.
    delta = task.days_until_due(AS_OF)
    if delta is not None and delta < 0:
        assert decision.kind is not DecisionKind.NO_ACTION


@settings(max_examples=300)
@given(records, interpretations, screens)
def test_8_llm_cannot_widen_the_decision(
    record: dict[str, Any],
    interpretation: NoteInterpretation | None,
    screen: ScreenResult,
) -> None:
    task = to_task(record)
    baseline = POLICY.decide(task, as_of=AS_OF)
    steered = POLICY.decide(
        task, as_of=AS_OF, screen=screen, interpretation=interpretation
    )
    if baseline.rule_id in DECIDED_EARLY:
        assert steered.kind is baseline.kind
    elif baseline.actionable:
        assert steered.actionable
    else:
        # A model can at most ask the warehouse for a delivery date.
        assert steered.kind in {
            DecisionKind.NO_ACTION,
            DecisionKind.REQUEST_ETA,
        }


def noisy(text: str) -> str:
    # Upper-casing is noise only when folding undoes it: a dotless
    # i upper-cases into a different letter.
    shouted = text.upper()
    if shouted.casefold() != text.casefold():
        shouted = text
    return f"  {shouted}  ".replace(" ", "   ")


@given(records)
def test_6_fingerprint_ignores_noise_and_tracks_changes(
    record: dict[str, Any],
) -> None:
    task = to_task(record)
    shaken = dict(record)
    shaken["task_id"] = " tc-1 "
    shaken["status"] = noisy(record["status"])
    shaken["notes"] = noisy(record["notes"])
    if record["assigned_to"] is not None:
        shaken["assigned_to"] = noisy(record["assigned_to"])
    shaken["last_update"] = None
    assert state_fingerprint(to_task(shaken)) == state_fingerprint(task)

    moved = dict(record)
    moved["due_date"] = "1999-01-01"
    assert state_fingerprint(to_task(moved)) != state_fingerprint(task)


_PLACEHOLDER_LEFT = re.compile(r"[{}]")
_LINK = re.compile(
    r"\b[a-z][a-z0-9+.-]*://|\bwww\.|[^\s@]+@[^\s@]+\.[^\s@]+", re.IGNORECASE
)


@settings(max_examples=300)
@given(
    records,
    st.text(max_size=120),
    st.sampled_from(["https://evil.example/x", "boss@evil.example", "{x}"]),
)
@pytest.mark.asvs("1.3.7", "1.3.11")
def test_9_rendered_messages_are_safe(
    record: dict[str, Any], title: str, payload: str
) -> None:
    record = {**record, "task": f"{title} {payload}", "project": payload}
    task = to_task(record)
    decision = POLICY.decide(task, as_of=AS_OF)
    if not decision.actionable:
        return
    draft = compose(decision, task, AS_OF)
    for text in (draft.subject, draft.body):
        assert not _PLACEHOLDER_LEFT.search(text)
        assert not _LINK.search(text)
        assert "\n" not in text
        assert "\r" not in text
    assert 1 <= len(draft.subject) <= SUBJECT_MAX_CHARS
    assert BODY_MIN_CHARS <= len(draft.body) <= BODY_MAX_CHARS


@settings(max_examples=500)
@given(
    st.lists(
        json_values
        | st.dictionaries(
            st.sampled_from(
                [
                    "task_id",
                    "project",
                    "task",
                    "assigned_to",
                    "due_date",
                    "status",
                    "last_update",
                    "notes",
                ]
            ),
            json_values | st.just("TC-7"),
        ),
        max_size=5,
    )
)
def test_10_normalize_is_total(raw: list[Any]) -> None:
    result = normalize(raw, AS_OF)
    assert len(result.tasks) + len(result.rejected) == len(raw)
    for item in result.rejected:
        assert isinstance(item, RejectedRecord)
