"""Plain helpers shared by the domain, property and golden tests.

Hypothesis tests cannot use function-scoped fixtures, so the helpers
live here as functions and ``conftest.py`` only wraps them.
"""

import dataclasses
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from techcom_followup.domain.injection import screen
from techcom_followup.domain.interpretation import (
    fallback,
    needs_llm,
    prefilter,
)
from techcom_followup.domain.models import (
    Decision,
    NoteInterpretation,
    Task,
    TaskId,
    TaskStatus,
    UntrustedText,
)
from techcom_followup.domain.policy import DecisionPolicy
from techcom_followup.infrastructure.policy_files import (
    load_decision_policy,
)

ROOT = Path(__file__).resolve().parent.parent
AS_OF = date(2026, 10, 21)

BASE_TASK = Task(
    task_id=TaskId("TC-1"),
    project="SURF Illinois",
    title="Fiber splicing",
    owner="John Smith",
    due_date=date(2026, 10, 30),
    status=TaskStatus.WORKING_ON_IT,
    last_update=date(2026, 10, 20),
    notes=UntrustedText(""),
)


def make_task(**changes: Any) -> Task:
    """Return an on-track task with ``changes`` applied."""
    if isinstance(changes.get("notes"), str):
        changes["notes"] = UntrustedText.of(changes["notes"])
    return dataclasses.replace(BASE_TASK, **changes)


def repo_policy() -> DecisionPolicy:
    """Return the policy built from the committed ``config/`` files."""
    return load_decision_policy(ROOT / "config")


@dataclass(frozen=True, slots=True)
class Assessment:
    decision: Decision
    llm_needed: bool


def assess(
    policy: DecisionPolicy,
    task: Task,
    as_of: date = AS_OF,
    llm: NoteInterpretation | None = None,
) -> Assessment:
    """Run the pure part of the pipeline for one task.

    ``llm`` stands in for the model: it is used only when the gate
    says a model call is needed; ``None`` means "the call failed".
    """
    result = screen(task.notes)
    hit = None if result.suspicious else prefilter(task.notes)
    wanted = needs_llm(task, result, hit, as_of=as_of, due_soon_days=1)
    interpretation = (llm or fallback()) if wanted else hit
    decision = policy.decide(
        task, as_of=as_of, screen=result, interpretation=interpretation
    )
    return Assessment(decision, wanted)
