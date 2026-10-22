from typing import Any

import openai
import pytest
from openai.types.responses import Response

import llm_doubles as sdk
from techcom_followup.adapters.outbound.llm.config import (
    RouteConfig,
    RoutingConfig,
)
from techcom_followup.adapters.outbound.llm.openai_responses import (
    BackendError,
    FailureKind,
    OpenAIResponsesBackend,
)
from techcom_followup.application.ports import (
    LLMConfigurationError,
    LLMIncompleteError,
    LLMPurpose,
    LLMRefusalError,
    LLMRequest,
)


class RecordingCreate:
    """Stands in for ``AsyncOpenAI().responses.create``."""

    def __init__(self, result: Response | Exception) -> None:
        self.result = result
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, **kwargs: Any) -> Response:
        self.calls.append(kwargs)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


@pytest.fixture
def route(routing_config: RoutingConfig) -> RouteConfig:
    return routing_config.routes[LLMPurpose.INTERPRET_NOTE][0]


async def test_request_is_strict_private_and_single_shot(
    request_: LLMRequest,
    route: RouteConfig,
) -> None:
    create = RecordingCreate(
        sdk.response(
            output=[
                sdk.reasoning_item(),
                sdk.text_message('{"blocker": "none"}'),
            ]
        ),
    )
    backend = OpenAIResponsesBackend(create)

    reply = await backend.complete(request_, route, timeout_s=7.5)

    assert reply.text == '{"blocker": "none"}'
    assert reply.response_id == "resp_test_1"
    assert reply.usage.cached_input_tokens == 0
    assert reply.usage.reasoning_tokens == 50
    (kwargs,) = create.calls
    assert kwargs["model"] == "gpt-6.1-sol"
    assert kwargs["store"] is False
    assert kwargs["truncation"] == "disabled"
    assert kwargs["reasoning"] == {"effort": "low"}
    assert kwargs["timeout"] == 7.5
    assert kwargs["prompt_cache_key"] == "interpret_note:interp.v1"
    text_format = kwargs["text"]["format"]
    assert text_format["type"] == "json_schema"
    assert text_format["strict"] is True
    assert text_format["schema"] == dict(request_.json_schema)
    assert "safety_identifier" not in kwargs


async def test_safety_identifier_is_forwarded(
    request_: LLMRequest,
    route: RouteConfig,
) -> None:
    create = RecordingCreate(sdk.response(output=[sdk.text_message("{}")]))
    backend = OpenAIResponsesBackend(create)
    tagged = LLMRequest(
        purpose=request_.purpose,
        instructions=request_.instructions,
        input_text=request_.input_text,
        schema_name=request_.schema_name,
        json_schema=request_.json_schema,
        prompt_version=request_.prompt_version,
        safety_identifier="svc-followup-hash",
    )
    await backend.complete(tagged, route, timeout_s=5)
    assert create.calls[0]["safety_identifier"] == "svc-followup-hash"


async def test_refusal_is_reported_with_usage(
    request_: LLMRequest,
    route: RouteConfig,
) -> None:
    backend = OpenAIResponsesBackend(
        RecordingCreate(sdk.response(output=[sdk.refusal_message()])),
    )
    with pytest.raises(LLMRefusalError) as caught:
        await backend.complete(request_, route, timeout_s=5)
    assert caught.value.usage is not None


@pytest.mark.parametrize(
    ("status", "with_details", "reason", "expected"),
    [
        ("incomplete", True, "max_output_tokens", "max_output_tokens"),
        ("incomplete", True, None, "incomplete"),
        ("failed", False, None, "failed"),
    ],
)
async def test_incomplete_responses_fail_closed(
    request_: LLMRequest,
    route: RouteConfig,
    status: str,
    with_details: bool,
    reason: sdk.IncompleteReason | None,
    expected: str,
) -> None:
    result = sdk.response(
        output=[sdk.text_message('{"blocker": "mat')],
        status=status,
        with_details=with_details,
        incomplete_reason=reason,
    )
    backend = OpenAIResponsesBackend(RecordingCreate(result))
    with pytest.raises(LLMIncompleteError) as caught:
        await backend.complete(request_, route, timeout_s=5)
    assert caught.value.reason == expected


async def test_empty_output_fails_closed(
    request_: LLMRequest,
    route: RouteConfig,
) -> None:
    backend = OpenAIResponsesBackend(
        RecordingCreate(sdk.response(output=[sdk.text_message("  ")])),
    )
    with pytest.raises(LLMIncompleteError, match="empty_output"):
        await backend.complete(request_, route, timeout_s=5)


async def test_missing_usage_is_billed_conservatively(
    request_: LLMRequest,
    route: RouteConfig,
) -> None:
    backend = OpenAIResponsesBackend(
        RecordingCreate(
            sdk.response(output=[sdk.text_message("{}")], with_usage=False)
        ),
    )
    reply = await backend.complete(request_, route, timeout_s=5)
    assert reply.usage.output_tokens == route.max_output_tokens
    prompt = len(request_.instructions) + len(request_.input_text)
    assert reply.usage.input_tokens > prompt // 4


@pytest.mark.parametrize(
    ("error", "kind"),
    [
        (sdk.timeout_error(), FailureKind.TRANSIENT),
        (sdk.connection_error(), FailureKind.TRANSIENT),
        (sdk.status_error(openai.RateLimitError, 429), FailureKind.TRANSIENT),
        (sdk.status_error(openai.ConflictError, 409), FailureKind.TRANSIENT),
        (
            sdk.status_error(openai.InternalServerError, 503),
            FailureKind.TRANSIENT,
        ),
        (sdk.status_error(openai.NotFoundError, 404), FailureKind.REJECTED),
        (
            sdk.status_error(openai.PermissionDeniedError, 403),
            FailureKind.REJECTED,
        ),
        (sdk.status_error(openai.BadRequestError, 400), FailureKind.REJECTED),
        (sdk.validation_error(), FailureKind.REJECTED),
    ],
)
async def test_sdk_errors_map_to_failover_kinds(
    request_: LLMRequest,
    route: RouteConfig,
    error: Exception,
    kind: FailureKind,
) -> None:
    backend = OpenAIResponsesBackend(RecordingCreate(error))
    with pytest.raises(BackendError) as caught:
        await backend.complete(request_, route, timeout_s=5)
    assert caught.value.kind is kind


async def test_bad_credentials_are_a_configuration_error(
    request_: LLMRequest,
    route: RouteConfig,
) -> None:
    error = sdk.status_error(openai.AuthenticationError, 401)
    backend = OpenAIResponsesBackend(RecordingCreate(error))
    with pytest.raises(LLMConfigurationError):
        await backend.complete(request_, route, timeout_s=5)


async def test_missing_refusal_usage_remains_unknown_for_reservation(
    request_: LLMRequest, route: RouteConfig
) -> None:
    backend = OpenAIResponsesBackend(
        RecordingCreate(
            sdk.response(output=[sdk.refusal_message()], with_usage=False)
        )
    )
    with pytest.raises(LLMRefusalError) as caught:
        await backend.complete(request_, route, timeout_s=5)
    assert caught.value.usage is None
