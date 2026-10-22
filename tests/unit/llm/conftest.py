from pathlib import Path

import pytest
from llm_doubles import FakeClock

from techcom_followup.adapters.outbound.llm.config import (
    RoutingConfig,
    load_routing_config,
)
from techcom_followup.application.ports import LLMPurpose, LLMRequest

ROOT = Path(__file__).resolve().parents[3]


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
