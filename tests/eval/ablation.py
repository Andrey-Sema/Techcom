"""Measure what the model adds: the same corpus with no model at all.

Without a client the interpreter and drafter take the offline path the
service uses when the model is unavailable: keyword hits, the
``UNCLEAR`` fallback and catalogue templates. Policy, routing and the
safety judge are the replay harness's own.
"""

from dataclasses import dataclass

from eval import harness
from eval.contracts import EvalCase
from eval.metrics import CaseResult, Scores, score
from techcom_followup.application.drafter import MessageDrafter
from techcom_followup.application.interpreter import NoteInterpreter
from techcom_followup.domain.injection import screen
from techcom_followup.domain.models import BlockerCategory


async def without_model(case: EvalCase) -> CaseResult:
    """Run one case through the offline path only."""
    task = harness.task_for(case)
    screened = screen(task.notes)
    interpreter = NoteInterpreter(
        None, now=lambda: harness.NOW, monotonic=lambda: 0
    )
    interpretation = await interpreter.interpret(
        task, as_of=harness.AS_OF, screen=screened
    )
    decision = harness._POLICY.decide(
        task,
        as_of=harness.AS_OF,
        screen=screened,
        interpretation=interpretation,
    )
    draft = (
        await MessageDrafter(None).draft(decision, task, as_of=harness.AS_OF)
        if decision.actionable
        else None
    )
    return CaseResult(
        case_id=case.case_id,
        expected=case.label,
        predicted=BlockerCategory.NONE
        if interpretation is None
        else interpretation.blocker,
        expected_decision=case.expected_decision,
        actual_decision=decision.kind,
        injection=case.injection,
        screened=screened.suspicious,
        interpreter_calls=0,
        drafting_calls=0,
        failures=0,
        unsafe_reasons=harness._safe_reasons(
            task,
            decision,
            draft,
            harness.ReplayClient(()),
            screened.suspicious,
        ),
    )


async def evaluate_without_model(
    cases: tuple[EvalCase, ...],
) -> tuple[CaseResult, ...]:
    """Run every case through the offline path."""
    return tuple([await without_model(case) for case in cases])


@dataclass(frozen=True, slots=True)
class Change:
    """A case whose decision differs with and without the model."""

    case_id: str
    expected: str
    without: str
    with_model: str

    @property
    def verdict(self) -> str:
        """Say whether the model moved the decision to the label."""
        if self.with_model == self.expected:
            return "fixed by the model"
        if self.without == self.expected:
            return "broken by the model"
        return "wrong either way"


def changes(
    without: tuple[CaseResult, ...], with_model: tuple[CaseResult, ...]
) -> tuple[Change, ...]:
    """List decision changes in corpus order."""
    offline = {row.case_id: row for row in without}
    return tuple(
        Change(
            row.case_id,
            row.expected_decision.value,
            offline[row.case_id].actual_decision.value,
            row.actual_decision.value,
        )
        for row in with_model
        if offline[row.case_id].actual_decision != row.actual_decision
    )


def _row(name: str, scores: Scores, benign: Scores) -> str:
    return (
        f"| {name} | {scores.macro_f1:.3f} | {benign.macro_f1:.3f} | "
        f"{scores.decision_mismatches} | {scores.unsafe_outcomes} |"
    )


def value_markdown(
    without: tuple[CaseResult, ...], with_model: harness.Evaluation
) -> str:
    """Render the with/without comparison as deterministic Markdown."""
    found = changes(without, with_model.results)
    tally = {
        verdict: sum(change.verdict == verdict for change in found)
        for verdict in (
            "fixed by the model",
            "broken by the model",
            "wrong either way",
        )
    }
    models = ", ".join(sorted(with_model.models)) or "none"
    lines = [
        "# Model value: the same corpus with and without the model",
        "",
        f"Dataset: `{with_model.manifest.dataset_version}`.",
        (
            f"Responses: `{models}`; provider quality "
            f"**{with_model.provider_quality}**."
        ),
        "",
        "Without the model, notes go through keyword hits, the `unclear`",
        "fallback and catalogue templates: the service's offline path.",
        "Policy, routing and the safety judge are identical in both rows.",
        "",
        (
            "| Pipeline | Macro-F1 | Benign macro-F1 | Wrong decisions "
            "| Unsafe outcomes |"
        ),
        "|---|---:|---:|---:|---:|",
        _row(
            "Without the model",
            score(without),
            score(tuple(row for row in without if not row.injection)),
        ),
        _row(
            "With the model",
            with_model.scores,
            score(
                tuple(row for row in with_model.results if not row.injection)
            ),
        ),
        "",
        "## Decisions the model changed",
        "",
        f"- Fixed by the model: {tally['fixed by the model']}",
        f"- Broken by the model: {tally['broken by the model']}",
        f"- Wrong either way: {tally['wrong either way']}",
        "",
    ]
    lines.extend(
        f"- `{change.case_id}`: `{change.without}` → `{change.with_model}` "
        f"(label `{change.expected}`, {change.verdict})."
        for change in found
    )
    if not found:
        lines.append("The model changed no final decision.")
    return "\n".join(lines) + "\n"
