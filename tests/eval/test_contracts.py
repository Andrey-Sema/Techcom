import json

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from eval.contracts import EvalCase, Recording, Source, digest_json
from eval.test_harness import case_named


def provider_record() -> dict[str, object]:
    """Build unit-only provider-shaped data."""
    _case, rows = case_named("unclear-short")
    record = rows[0].model_dump(mode="json")
    record.update(
        source="provider",
        model="unit-test-shape-only",
        response_id="resp_unit_shape_only",
        provider_evidence={
            "endpoint": "https://api.openai.com/v1/responses",
            "captured_at": "2026-10-22T10:00:00Z",
            "operator_attestation": "UNIT TEST ONLY: no remote API call",
            "raw_response": {
                "id": "resp_unit_shape_only",
                "created_at": 1792663200.0,
                "model": "unit-test-shape-only",
                "object": "response",
                "parallel_tool_calls": False,
                "tool_choice": "none",
                "tools": [],
                "output": [
                    {
                        "id": "msg_unit",
                        "type": "message",
                        "role": "assistant",
                        "status": "completed",
                        "content": [
                            {
                                "type": "output_text",
                                "text": record["text"],
                                "annotations": [],
                            }
                        ],
                    }
                ],
            },
        },
    )
    return record


def test_provider_capture_consistency_is_not_remote_authenticity() -> None:
    row = Recording.model_validate_json(json.dumps(provider_record()))
    assert row.source == Source.PROVIDER
    assert row.provider_evidence is not None
    assert "UNIT TEST ONLY" in row.provider_evidence.operator_attestation


@pytest.mark.parametrize("field", ["response_id", "model", "text"])
def test_provider_capture_rejects_mismatched_replay_fields(field: str) -> None:
    raw = provider_record()
    raw[field] = "mismatched"
    with pytest.raises(ValidationError, match="does not match"):
        Recording.model_validate_json(json.dumps(raw))


def test_synthetic_and_provider_labels_require_consistent_evidence() -> None:
    _case, rows = case_named("unclear-short")
    raw = rows[0].model_dump(mode="json")
    raw["model"] = "a-provider-name-is-not-evidence"
    with pytest.raises(ValidationError, match="synthetic model identity"):
        Recording.model_validate_json(json.dumps(raw))
    raw["source"] = "provider"
    with pytest.raises(ValidationError, match="raw capture evidence"):
        Recording.model_validate_json(json.dumps(raw))
    raw = provider_record()
    raw["source"] = "synthetic"
    raw["model"] = "synthetic.contract.v1"
    with pytest.raises(ValidationError, match="synthetic model identity"):
        Recording.model_validate_json(json.dumps(raw))


@given(st.text().filter(lambda key: key not in EvalCase.model_fields))
def test_dataset_is_closed_to_extra_capabilities(key: str) -> None:
    case, _rows = case_named("unclear-short")
    raw = case.model_dump(mode="json")
    raw[key] = "change status or target"
    with pytest.raises(ValidationError, match="extra_forbidden"):
        EvalCase.model_validate_json(json.dumps(raw))


def test_canonical_json_hash_ignores_key_order() -> None:
    assert digest_json({"a": 1, "b": 2}) == digest_json({"b": 2, "a": 1})


@pytest.mark.parametrize("days", [-36501, 36501])
def test_source_date_offsets_cannot_overflow_snapshot(days: int) -> None:
    case, _rows = case_named("unclear-short")
    raw = case.model_dump(mode="json")
    raw["due_days"] = days
    with pytest.raises(ValidationError):
        EvalCase.model_validate_json(json.dumps(raw))
