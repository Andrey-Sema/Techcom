"""The offline orchestrator model follows the step protocol."""

import json

import pytest
from pydantic import JsonValue

from techcom_followup.adapters.outbound.llm_agent_stub import (
    OfflineLanguageModel,
    next_step,
)
from techcom_followup.application.ports import (
    LLMError,
    LLMPurpose,
    LLMRequest,
)

CONTEXT: dict[str, JsonValue] = {
    "envelope": {
        "baseline": {
            "decision": "follow_up_owner",
            "recipient": "owner",
            "priority": "normal",
        },
        "allowed": {"follow_up_owner": ["owner"]},
    }
}


def request(purpose: LLMPurpose) -> LLMRequest:
    payload = {"context": CONTEXT, "transcript": [], "final_step": False}
    return LLMRequest(
        purpose=purpose,
        instructions="",
        input_text=json.dumps(payload),
        schema_name="step",
        json_schema={},
        prompt_version="v",
    )


async def test_other_purposes_fail_closed() -> None:
    model = OfflineLanguageModel()
    with pytest.raises(LLMError, match="no offline model"):
        await model.generate_json(request(LLMPurpose.INTERPRET_NOTE))
    result = await model.generate_json(request(LLMPurpose.ORCHESTRATE))
    assert json.loads(result.text) == {"tool": "get_history", "plan": None}
    assert result.provider == "offline"


def test_final_step_submits_without_history() -> None:
    step = next_step(
        {"context": CONTEXT, "transcript": [], "final_step": True}
    )
    assert step["tool"] == "submit_plan"
    plan = step["plan"]
    assert isinstance(plan, dict)
    assert plan["decision"] == "follow_up_owner"
    assert plan["evidence"] == []
