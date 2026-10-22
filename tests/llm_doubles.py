"""Builders for real OpenAI SDK objects used as test doubles.

Using the SDK's own types (not hand-made dicts) keeps the tests honest
about the attribute names the backend reads.
"""

from collections.abc import Sequence
from typing import Any, Literal

import httpx2
import openai
from openai.types.responses import (
    Response,
    ResponseOutputMessage,
    ResponseOutputRefusal,
    ResponseOutputText,
    ResponseReasoningItem,
    ResponseUsage,
)
from openai.types.responses.response import IncompleteDetails
from openai.types.responses.response_usage import (
    InputTokensDetails,
    OutputTokensDetails,
)

type IncompleteReason = Literal[
    "max_output_tokens",
    "max_messages",
    "content_filter",
    "steered",
]

REQUEST = httpx2.Request("POST", "https://api.openai.com/v1/responses")


def usage(
    *,
    input_tokens: int = 1_000,
    output_tokens: int = 200,
    cached: int = 0,
    cache_write: int = 0,
    reasoning: int = 50,
) -> ResponseUsage:
    return ResponseUsage.model_construct(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=input_tokens + output_tokens,
        input_tokens_details=InputTokensDetails.model_construct(
            cached_tokens=cached,
            cache_write_tokens=cache_write,
        ),
        output_tokens_details=OutputTokensDetails.model_construct(
            reasoning_tokens=reasoning,
        ),
    )


def text_message(text: str) -> ResponseOutputMessage:
    return ResponseOutputMessage.model_construct(
        id="msg_1",
        role="assistant",
        status="completed",
        type="message",
        content=[
            ResponseOutputText.model_construct(
                type="output_text",
                text=text,
                annotations=[],
            ),
        ],
    )


def refusal_message() -> ResponseOutputMessage:
    return ResponseOutputMessage.model_construct(
        id="msg_2",
        role="assistant",
        status="completed",
        type="message",
        content=[
            ResponseOutputRefusal.model_construct(
                type="refusal",
                refusal="I cannot help with that.",
            ),
        ],
    )


def reasoning_item() -> ResponseReasoningItem:
    return ResponseReasoningItem.model_construct(
        id="rs_1",
        type="reasoning",
        summary=[],
    )


def response(
    *,
    output: Sequence[object] = (),
    status: str = "completed",
    incomplete_reason: IncompleteReason | None = None,
    with_details: bool = False,
    with_usage: bool = True,
    model: str = "gpt-6.1-sol",
) -> Response:
    details = (
        IncompleteDetails.model_construct(reason=incomplete_reason)
        if with_details
        else None
    )
    fields: dict[str, Any] = {
        "id": "resp_test_1",
        "model": model,
        "status": status,
        "output": list(output),
        "incomplete_details": details,
        "usage": usage() if with_usage else None,
    }
    return Response.model_construct(**fields)


def status_error(
    cls: type[openai.APIStatusError], code: int
) -> openai.APIStatusError:
    return cls(
        "error",
        response=httpx2.Response(code, request=REQUEST),
        body=None,
    )


def validation_error() -> openai.APIResponseValidationError:
    return openai.APIResponseValidationError(
        httpx2.Response(200, request=REQUEST),
        None,
    )


def timeout_error() -> openai.APITimeoutError:
    return openai.APITimeoutError(request=REQUEST)


def connection_error() -> openai.APIConnectionError:
    return openai.APIConnectionError(request=REQUEST)


class FakeClock:
    """Monotonic clock and calendar that tests move by hand."""

    def __init__(self) -> None:
        self.now = 1_000.0
        self.day_offset = 0

    def monotonic(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds
