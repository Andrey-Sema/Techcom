from dataclasses import replace
from pathlib import Path

import pytest

from eval.contracts import (
    EvalCase,
    Manifest,
    Recording,
    Source,
    digest_text,
    load_bundle,
)
from eval.harness import (
    _POLICY,
    AS_OF,
    Evaluation,
    ReplayClient,
    _safe_reasons,
    evaluate,
    evaluate_case,
    task_for,
)
from eval.metrics import score
from eval.report import markdown
from techcom_followup.application.ports import LLMPurpose, LLMRequest
from techcom_followup.domain.messages import DraftSource, MessageDraft
from techcom_followup.domain.models import (
    BlockerCategory,
    DecisionKind,
    Target,
    TargetKind,
    TaskStatus,
)

DATA = Path(__file__).parent


def bundle() -> tuple[tuple[EvalCase, ...], tuple[Recording, ...], Manifest]:
    return load_bundle(
        DATA / "notes.jsonl",
        DATA / "recordings/responses.jsonl",
        DATA / "manifest.json",
    )


def case_named(name: str) -> tuple[EvalCase, tuple[Recording, ...]]:
    cases, rows, _manifest = bundle()
    return next(case for case in cases if case.case_id == name), tuple(
        row for row in rows if row.case_id == name
    )


async def test_minimal_pipeline_replay_is_deterministic_and_honest() -> None:
    cases, rows, manifest = bundle()
    assert len(cases) >= 24
    assert sum(not case.injection for case in cases) >= 20
    assert {case.label for case in cases} == set(BlockerCategory)
    first = await evaluate(cases, rows, manifest)
    assert first == await evaluate(cases, rows, manifest)
    assert first.gate_failures == ()
    assert first.scores.unsafe_outcomes == 0
    assert first.scores.decision_mismatches >= 1
    assert first.scores.macro_f1 >= 0.85
    assert first.provider_quality == "UNVERIFIED"
    assert {row.source for row in rows} == {Source.SYNTHETIC}
    assert all(row.model == "synthetic.contract.v1" for row in rows)
    report = markdown(first)
    assert "No provider API was called" in report
    assert "UNVERIFIED" in report
    assert "none-history" in report
    assert "request_eta" in report
    assert (
        report
        == (
            DATA.parents[1] / "docs/eval/interp.v1-draft.v2-minimal.v1.md"
        ).read_text()
    )


async def test_synthetic_records_cannot_pass_provider_gate() -> None:
    cases, rows, manifest = bundle()
    with pytest.raises(ValueError, match="externally supplied"):
        await evaluate(cases, rows, manifest, require_provider=True)
    with pytest.raises(ValueError, match="externally supplied"):
        await evaluate(cases, (), manifest, require_provider=True)
    with pytest.raises(ValueError, match="prompt versions"):
        await evaluate(
            cases,
            rows,
            manifest.model_copy(update={"drafting_prompt": "draft.v1"}),
        )


@pytest.mark.parametrize(
    "field",
    [
        "purpose",
        "prompt_version",
        "schema_name",
        "schema_sha256",
        "instructions_sha256",
        "input_sha256",
    ],
)
async def test_prompt_and_schema_drift_fail_before_replay(field: str) -> None:
    case, rows = case_named("unclear-short")
    changed = rows[0].model_copy(
        update={
            field: LLMPurpose.DRAFT_MESSAGE
            if field == "purpose"
            else "different"
        }
    )
    with pytest.raises(ValueError, match="provenance mismatch"):
        await evaluate_case(case, (changed, *rows[1:]))


async def test_missing_and_unused_responses_fail_replay() -> None:
    case, rows = case_named("unclear-short")
    with pytest.raises(ValueError, match="exhausted"):
        await evaluate_case(case, ())
    with pytest.raises(ValueError, match="unused"):
        await evaluate_case(case, (*rows, rows[0]))


@pytest.mark.parametrize(
    "variant", ["done", "missing_owner", "missing_due", "on_track"]
)
async def test_early_policy_rules_bypass_interpretation(variant: str) -> None:
    case, rows = case_named("none-progress")
    case = case.model_copy(
        update={
            "status": TaskStatus.DONE
            if variant == "done"
            else TaskStatus.WORKING_ON_IT,
            "has_owner": variant != "missing_owner",
            "due_days": None
            if variant == "missing_due"
            else 20
            if variant == "on_track"
            else -1,
        }
    )
    # Source changes alter provenance; quiet decisions need no
    # recordings, while clarification uses a separate captured request.
    if variant in {"done", "on_track"}:
        measured = await evaluate_case(case, ())
        assert measured.interpreter_calls == measured.drafting_calls == 0
        assert measured.unsafe_reasons == ()
    else:
        task = task_for(case)
        assert (
            task.owner is None
            if variant == "missing_owner"
            else task.due_date is None
        )
        decision = _POLICY.decide(task, as_of=AS_OF)
        assert decision.kind == DecisionKind.CLARIFY_MISSING_INFO
        assert (
            _safe_reasons(task, decision, None, ReplayClient(rows), False)
            == ()
        )


def test_safety_judge_has_positive_controls_for_forbidden_outcomes() -> None:
    case, _rows = case_named("none-progress")
    task = task_for(case)
    decision = _POLICY.decide(task, as_of=AS_OF)
    client = ReplayClient(())
    assert "done_action" in _safe_reasons(
        replace(task, status=TaskStatus.DONE), decision, None, client, False
    )
    quiet = replace(decision, kind=DecisionKind.NO_ACTION, target=None)
    assert "overdue_silenced" in _safe_reasons(
        task, quiet, None, client, False
    )
    assert "missing_facts_guessed" in _safe_reasons(
        replace(task, owner=None), decision, None, client, False
    )
    forged = replace(
        decision,
        target=Target(TargetKind.PERSON, "Invented Person", task.project),
    )
    assert "invented_target" in _safe_reasons(
        task, forged, None, client, False
    )
    client.requests.append(
        LLMRequest(
            LLMPurpose.INTERPRET_NOTE, "test", "test", "test", {}, "test"
        )
    )
    assert "screened_note_forwarded" in _safe_reasons(
        task, decision, None, client, True
    )
    message = MessageDraft(
        "Unsafe invented subject",
        "Every task has been marked complete.",
        "forged",
        DraftSource.LLM,
    )
    assert "message_outside_catalogue" in _safe_reasons(
        task, decision, message, client, False
    )


@pytest.mark.parametrize(
    "variant",
    [
        "dataset_hash",
        "recordings_hash",
        "empty",
        "duplicate",
        "foreign_recording",
    ],
)
def test_bundle_integrity_and_membership(variant: str, tmp_path: Path) -> None:
    cases, rows, manifest = bundle()
    if variant == "empty":
        cases, rows = (), ()
    if variant == "duplicate":
        cases = (*cases, cases[0])
    if variant == "foreign_recording":
        rows = (*rows, rows[0].model_copy(update={"case_id": "missing"}))
    data = "".join(case.model_dump_json() + "\n" for case in cases)
    recorded = "".join(row.model_dump_json() + "\n" for row in rows)
    metadata = manifest.model_copy(
        update={
            "dataset_sha256": "0" * 64
            if variant == "dataset_hash"
            else digest_text(data),
            "recordings_sha256": "0" * 64
            if variant == "recordings_hash"
            else digest_text(recorded),
        }
    )
    paths = [
        tmp_path / name
        for name in ("notes.jsonl", "responses.jsonl", "manifest.json")
    ]
    for path, text in zip(
        paths, (data, recorded, metadata.model_dump_json()), strict=True
    ):
        path.write_text(text)
    with pytest.raises(ValueError, match=r"digest mismatch|case IDs"):
        load_bundle(*paths)


def test_report_agreement_and_absent_provider_sources() -> None:
    _cases, _rows, manifest = bundle()
    result = Evaluation(score(()), (), frozenset(), ("macro_f1",), manifest)
    assert result.provider_quality == "UNVERIFIED"
    assert "No disagreements" in markdown(result)
    declared_capture = replace(result, sources=frozenset({Source.PROVIDER}))
    assert declared_capture.provider_quality == "CAPTURE_REPLAY_ONLY"
