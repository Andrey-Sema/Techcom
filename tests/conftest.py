"""Fixtures shared by the whole test suite."""

import json
from datetime import date

import pytest

from llm_doubles import FakeClock
from support import AS_OF, ROOT, repo_policy
from techcom_followup.adapters.outbound.llm.config import (
    RoutingConfig,
    load_routing_config,
)
from techcom_followup.application.ports import LLMPurpose, LLMRequest
from techcom_followup.domain.policy import DecisionPolicy


@pytest.fixture
def as_of() -> date:
    return AS_OF


@pytest.fixture
def policy() -> DecisionPolicy:
    return repo_policy()


@pytest.fixture
def fixture_records() -> list[object]:
    text = (ROOT / "fixtures" / "tasks.json").read_text(encoding="utf-8")
    records: list[object] = json.loads(text)
    return records


@pytest.fixture
def routing_config() -> RoutingConfig:
    return load_routing_config(ROOT / "config" / "llm.toml")


@pytest.fixture
def request_() -> LLMRequest:
    return LLMRequest(
        purpose=LLMPurpose.INTERPRET_NOTE,
        instructions="Classify the note. Output JSON only.",
        input_text='<task_note id="TC-1008">Unable to proceed</task_note>',
        schema_name="note_interpretation",
        json_schema={
            "type": "object",
            "properties": {"blocker": {"type": "string"}},
            "required": ["blocker"],
            "additionalProperties": False,
        },
        prompt_version="interp.v1",
    )


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()
