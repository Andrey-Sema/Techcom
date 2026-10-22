import json
from uuid import UUID

import pytest
import structlog
from hypothesis import given
from hypothesis import strategies as st

from techcom_followup.infrastructure.log_config import (
    configure_logging,
    redact_event,
)


@given(st.dictionaries(st.text(), st.text(), max_size=20))
def test_untrusted_metadata_cannot_change_closed_log_projection(
    extra: dict[str, str],
) -> None:
    trusted = {
        "event": "llm.validation_failed",
        "stage": "schema",
        "prompt_version": "interp.v1",
        "output_sha256": "a" * 64,
        "tokens_in": 12,
    }
    untrusted = {
        key: value for key, value in extra.items() if key not in trusted
    }
    assert redact_event(None, "warning", trusted | untrusted) == redact_event(
        None, "warning", trusted
    )


@pytest.mark.parametrize("invalid", [None, 123, "prompt SECRET", [], {}])
def test_unrecognized_names_and_levels_never_reach_logs(
    invalid: object,
) -> None:
    assert redact_event(None, "SECRET", {"event": invalid}) == {
        "event": "log.redacted",
        "level": "info",
    }


@pytest.mark.parametrize("value", [-1, True, 2**63, "12", None])
def test_counters_reject_unbounded_or_inexact_values(value: object) -> None:
    assert "tokens_in" not in redact_event(
        None, "info", {"event": "llm.call", "tokens_in": value}
    )


@pytest.mark.parametrize("value", [-1.0, float("nan"), float("inf"), 1, None])
def test_latency_rejects_invalid_measurements(value: object) -> None:
    assert "latency_s" not in redact_event(None, "info", {"latency_s": value})


@pytest.mark.parametrize("digest", ["A" * 64, "a" * 63, "a" * 65, None, []])
def test_validation_metadata_is_closed_and_digest_is_full_length(
    digest: object,
) -> None:
    assert redact_event(
        None,
        "error",
        {
            "event": "llm.validation_failed",
            "stage": "raw private text",
            "prompt_version": "private-version",
            "output_sha256": digest,
        },
    ) == {"event": "llm.validation_failed", "level": "error"}


def test_uuid_is_typed_and_safe_fields_keep_operational_evidence() -> None:
    identity = UUID(int=1)
    assert redact_event(
        None,
        "info",
        {
            "event": "command.completed",
            "run_id": identity,
            "latency_s": 0.5,
            "delivered": 0,
            "stage": "schema",
            "output_sha256": "a" * 64,
        },
    ) == {
        "event": "command.completed",
        "level": "info",
        "run_id": str(identity),
        "latency_s": 0.5,
        "delivered": 0,
    }
    assert "run_id" not in redact_event(None, "info", {"run_id": "private"})


def test_logging_emits_one_json_line_without_note_or_exception(
    capsys: pytest.CaptureFixture[str],
) -> None:
    previous = structlog.get_config()
    try:
        configure_logging()
        structlog.get_logger().warning(
            "llm.validation_failed",
            stage="json",
            prompt_version="draft.v2",
            output_sha256="b" * 64,
            note="PRIVATE\nsecond line",
            exc_info=True,
        )
    finally:
        structlog.configure(**previous)
    captured = capsys.readouterr()
    assert not captured.out
    assert captured.err.count("\n") == 1
    assert json.loads(captured.err) == {
        "event": "llm.validation_failed",
        "level": "warning",
        "stage": "json",
        "prompt_version": "draft.v2",
        "output_sha256": "b" * 64,
    }
