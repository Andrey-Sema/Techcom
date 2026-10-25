from dataclasses import replace
from pathlib import Path

import pytest
from openai.types.responses import Response

from eval.ablation import (
    Change,
    changes,
    evaluate_without_model,
    value_markdown,
)
from eval.cli import main
from eval.contracts import Recording, Source, load_bundle
from eval.harness import evaluate
from eval.metrics import score
from eval.provider import ENDPOINT, recording_from_capture
from eval.report import markdown
from eval.test_harness import bundle
from techcom_followup.application.ports import LLMPurpose, LLMRequest

DATA = Path(__file__).parent / "bundles" / "luna-high.v1"


def response(text: str) -> Response:
    """Build unit-only provider-shaped data with a schema format."""
    return Response.model_validate(
        {
            "id": "resp_unit_capture",
            "created_at": 1792663200.0,
            "model": "unit-test-shape-only",
            "object": "response",
            "parallel_tool_calls": False,
            "tool_choice": "none",
            "tools": [],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "note_interpretation",
                    "schema": {"type": "object"},
                    "strict": True,
                }
            },
            "output": [
                {
                    "id": "rs_unit",
                    "type": "reasoning",
                    "summary": [],
                    "encrypted_content": "opaque-organisation-bound-blob",
                },
                {
                    "id": "msg_unit",
                    "type": "message",
                    "role": "assistant",
                    "status": "completed",
                    "content": [
                        {
                            "type": "output_text",
                            "text": text,
                            "annotations": [],
                        }
                    ],
                },
            ],
        }
    )


def test_capture_binds_the_raw_response_to_its_request() -> None:
    request = LLMRequest(
        purpose=LLMPurpose.INTERPRET_NOTE,
        instructions="Classify the note.",
        input_text="<task_note>The kit is missing.</task_note>",
        schema_name="note_interpretation",
        json_schema={"type": "object"},
        prompt_version="interp.v1",
    )
    row = recording_from_capture(
        "unclear-short",
        request,
        response('{"blocker": "materials"}'),
        captured_at="2026-10-25T00:00:00+00:00",
        attestation="UNIT TEST ONLY: no remote API call",
    )
    row.verify_request(request)
    assert row.source is Source.PROVIDER
    assert row.text == '{"blocker": "materials"}'
    assert row.provider_evidence is not None
    assert row.provider_evidence.endpoint == ENDPOINT
    reasoning = row.provider_evidence.raw_response["output"]
    assert isinstance(reasoning, list)
    assert reasoning[0]["encrypted_content"] is None
    # The raw response must survive a round trip, including the
    # aliased ``schema`` field of a JSON-schema text format.
    assert Recording.model_validate_json(row.model_dump_json()) == row


async def test_without_model_keeps_every_safety_invariant() -> None:
    cases, _rows, _manifest = bundle()
    offline = await evaluate_without_model(cases)
    scores = score(offline)
    assert len(offline) == len(cases)
    assert scores.model_calls == 0
    assert scores.unsafe_outcomes == 0


async def test_value_report_counts_what_the_model_changed() -> None:
    cases, rows, manifest = bundle()
    replay = await evaluate(cases, rows, manifest)
    offline = await evaluate_without_model(cases)
    found = changes(offline, replay.results)
    assert found
    assert all(change.with_model != change.without for change in found)
    report = value_markdown(offline, replay)
    assert "| Without the model |" in report
    assert "- Fixed by the model:" in report
    unchanged = value_markdown(replay.results, replay)
    assert "The model changed no final decision." in unchanged


@pytest.mark.parametrize(
    ("without", "with_model", "verdict"),
    [
        ("follow_up_owner", "escalate", "fixed by the model"),
        ("escalate", "follow_up_owner", "broken by the model"),
        ("no_action", "follow_up_owner", "wrong either way"),
    ],
)
def test_change_verdicts(without: str, with_model: str, verdict: str) -> None:
    assert Change("case", "escalate", without, with_model).verdict == verdict


def test_cli_writes_the_model_value_report(tmp_path: Path) -> None:
    report = tmp_path / "value.md"
    assert main(["--value-report", str(report)]) == 0
    assert "# Model value" in report.read_text()


async def test_report_names_provider_captures_honestly() -> None:
    cases, rows, manifest = bundle()
    result = replace(
        await evaluate(cases, rows, manifest),
        sources=frozenset({Source.PROVIDER}),
    )
    text = markdown(result)
    assert "Captured provider responses are replayed offline" in text
    assert "passes `--require-provider`" in text
    assert "No provider API was called during replay" in text
    agreed = replace(
        result,
        results=tuple(
            row
            for row in result.results
            if row.expected == row.predicted
            and row.expected_decision == row.actual_decision
        ),
    )
    assert "No disagreements in this corpus." in markdown(agreed)


async def test_committed_luna_capture_replays_as_provider_evidence() -> None:
    cases, rows, manifest = load_bundle(
        DATA / "notes.jsonl",
        DATA / "recordings" / "responses.jsonl",
        DATA / "manifest.json",
    )
    result = await evaluate(cases, rows, manifest, require_provider=True)
    assert result.provider_quality == "CAPTURE_REPLAY_ONLY"
    assert result.models
    assert result.gate_failures == ()
    assert result.scores.unsafe_outcomes == 0
