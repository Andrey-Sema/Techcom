"""Deterministic offline stand-in for the orchestrator model.

It speaks the same JSON step protocol as a real model, so the demo and
tests exercise the whole loop (history, check, submit, guard) without
credentials or network. It reads only the code-owned part of the
context and never the untrusted note. Other purposes fail closed, as
an empty script does, so note interpretation takes its fallback.
"""

import json
from decimal import Decimal
from typing import cast

from pydantic import JsonValue

from techcom_followup.application.ports import (
    LLMError,
    LLMPurpose,
    LLMRequest,
    LLMResult,
    TokenUsage,
)

_PRIMARY = ("status", "delivery_eta", "missing_details", "unblock_plan")


def _asks(options: dict[str, list[str]]) -> list[JsonValue]:
    required = options["required_any"]
    primary = next((ask for ask in _PRIMARY if ask in required), required[0])
    if primary != "completion_date" and "completion_date" in options["asks"]:
        return [primary, "completion_date"]
    return [primary]


def _plan(
    context: dict[str, JsonValue], *, chased: bool
) -> dict[str, JsonValue]:
    envelope = cast("dict[str, JsonValue]", context["envelope"])
    baseline = cast("dict[str, str]", envelope["baseline"])
    allowed = cast("dict[str, list[str]]", envelope["allowed"])
    kind, recipient = baseline["decision"], baseline["recipient"]
    if chased and "escalate" in allowed and kind != "escalate":
        kind, recipient = "escalate", allowed["escalate"][0]
        reason = "An earlier follow-up was sent and the task is still late."
    else:
        reason = "The baseline action fits the facts."
    options = cast(
        "dict[str, dict[str, list[str]]]", envelope["message_options"]
    )[kind]
    tone = "firm" if chased and "firm" in options["tones"] else "neutral"
    return {
        "decision": kind,
        "recipient": recipient,
        "priority": "high" if kind == "escalate" else baseline["priority"],
        "asks": _asks(options),
        "tone": tone,
        "earlier_contact": bool(envelope["earlier_contact"]),
        "delivery": "send",
        "hold_reason": None,
        "rationale": reason,
        "evidence": [],
    }


def next_step(payload: dict[str, JsonValue]) -> dict[str, JsonValue]:
    """Choose the next tool call from code-owned context only."""
    transcript = cast("list[dict[str, JsonValue]]", payload["transcript"])
    if not transcript and not payload["final_step"]:
        return {"tool": "get_history", "plan": None}
    history = next(
        (
            cast("list[dict[str, str]]", item["result"])
            for item in transcript
            if item["tool"] == "get_history"
        ),
        [],
    )
    chased = any(entry["status"] == "sent" for entry in history)
    plan = _plan(
        cast("dict[str, JsonValue]", payload["context"]), chased=chased
    )
    checked = any(item["tool"] == "check_plan" for item in transcript)
    tool = "submit_plan" if checked or payload["final_step"] else "check_plan"
    return {"tool": tool, "plan": plan}


class OfflineLanguageModel:
    """Serve orchestrator steps offline; fail other purposes closed."""

    def __init__(self) -> None:
        """Count calls for tests and audit-free inspection."""
        self.calls = 0

    async def generate_json(self, request: LLMRequest) -> LLMResult:
        """Return the next orchestrator step as JSON text.

        Raises:
            LLMError: The purpose has no offline model.
        """
        if request.purpose is not LLMPurpose.ORCHESTRATE:
            message = "no offline model for this purpose"
            raise LLMError(message)
        self.calls += 1
        step = next_step(json.loads(request.input_text))
        return LLMResult(
            text=json.dumps(step),
            provider="offline",
            model="offline-agent.v1",
            response_id=f"offline-{self.calls}",
            usage=TokenUsage(0, 0),
            cost_usd=Decimal(0),
            latency_s=0,
        )
