"""OpenAI backend built on the Responses API (``openai`` SDK 3.x).

Design rules:

* One attempt per call: the SDK is created with ``max_retries=0`` and
  the router owns failover, so retries never multiply across layers.
* Structured output is requested with a strict JSON schema, but the text
  is returned raw; the application validates it with its own strict
  model (the provider schema is a convenience, not a guarantee).
* ``store=False``: prompts and outputs are not retained by the provider
  for later retrieval (data minimisation, ASVS 14.2.3).
* ``truncation="disabled"``: an oversized prompt fails loudly instead of
  being silently cut.
"""

from collections.abc import Awaitable, Callable
from enum import StrEnum

import openai
from openai.types.responses import Response

from techcom_followup.adapters.outbound.llm.accounting import maximum_usage
from techcom_followup.adapters.outbound.llm.config import RouteConfig
from techcom_followup.application.ports import (
    LLMConfigurationError,
    LLMIncompleteError,
    LLMRefusalError,
    LLMRequest,
    TokenUsage,
)

type ResponsesCreate = Callable[..., Awaitable[Response]]

_SERVER_ERROR = 500


class FailureKind(StrEnum):
    """Why a route failed; decides whether the router tries the next."""

    TRANSIENT = "transient"
    REJECTED = "rejected"


class BackendError(Exception):
    """A route-level failure the router may fail over from."""

    def __init__(self, kind: FailureKind, detail: str) -> None:
        """Keep the failure class and a non-sensitive detail string."""
        super().__init__(f"{kind}: {detail}")
        self.kind = kind
        self.detail = detail


class BackendReply:
    """Text and accounting extracted from a provider response."""

    __slots__ = ("model", "response_id", "text", "usage", "usage_reported")

    def __init__(
        self,
        *,
        text: str,
        model: str,
        response_id: str,
        usage: TokenUsage,
        usage_reported: bool = True,
    ) -> None:
        """Bundle the fields the router needs."""
        self.text = text
        self.model = model
        self.response_id = response_id
        self.usage = usage
        self.usage_reported = usage_reported


class OpenAIResponsesBackend:
    """Calls ``client.responses.create`` for one model route."""

    provider = "openai"

    def __init__(self, create: ResponsesCreate) -> None:
        """Wrap the SDK call (``AsyncOpenAI().responses.create``)."""
        self._create = create

    async def complete(
        self,
        request: LLMRequest,
        route: RouteConfig,
        timeout_s: float,
    ) -> BackendReply:
        """Run one request against one model.

        Raises:
            BackendError: Transient or rejected failure; try next route.
            LLMConfigurationError: Credentials are invalid.
            LLMRefusalError: The model refused.
            LLMIncompleteError: The output is incomplete or empty.
        """
        kwargs = _request_kwargs(request, route, timeout_s)
        try:
            response = await self._create(**kwargs)
        except openai.AuthenticationError as exc:
            msg = "OpenAI rejected the credentials"
            raise LLMConfigurationError(msg) from exc
        except openai.APIConnectionError as exc:
            kind = FailureKind.TRANSIENT
            raise BackendError(kind, type(exc).__name__) from exc
        except openai.APIStatusError as exc:
            raise _status_error(exc) from exc
        except openai.APIError as exc:
            kind = FailureKind.REJECTED
            raise BackendError(kind, type(exc).__name__) from exc
        fallback = _conservative_usage(request, route)
        return _reply_from(response, fallback)


def _request_kwargs(
    request: LLMRequest,
    route: RouteConfig,
    timeout_s: float,
) -> dict[str, object]:
    kwargs: dict[str, object] = {
        "model": route.model,
        "instructions": request.instructions,
        "input": request.input_text,
        "text": {
            "format": {
                "type": "json_schema",
                "name": request.schema_name,
                "schema": dict(request.json_schema),
                "strict": True,
            },
            "verbosity": route.verbosity,
        },
        "reasoning": {"effort": route.effort},
        "max_output_tokens": route.max_output_tokens,
        "service_tier": route.service_tier,
        "store": False,
        "truncation": "disabled",
        "prompt_cache_key": f"{request.purpose}:{request.prompt_version}",
        "timeout": timeout_s,
    }
    if request.safety_identifier is not None:
        kwargs["safety_identifier"] = request.safety_identifier
    return kwargs


def _status_error(exc: openai.APIStatusError) -> BackendError:
    transient = (
        isinstance(exc, (openai.RateLimitError, openai.ConflictError))
        or exc.status_code >= _SERVER_ERROR
    )
    kind = FailureKind.TRANSIENT if transient else FailureKind.REJECTED
    return BackendError(kind, f"http {exc.status_code}")


def _conservative_usage(request: LLMRequest, route: RouteConfig) -> TokenUsage:
    return maximum_usage(request, max_output_tokens=route.max_output_tokens)


def _reply_from(response: Response, fallback: TokenUsage) -> BackendReply:
    usage = _usage_from(response, fallback)
    billed_usage = usage if response.usage is not None else None
    for item in response.output:
        if item.type != "message":
            continue
        for content in item.content:
            if content.type == "refusal":
                msg = "model refused the request"
                raise LLMRefusalError(msg, billed_usage)
    if response.status != "completed":
        details = response.incomplete_details
        reason = (
            details.reason
            if details is not None and details.reason is not None
            else str(response.status)
        )
        raise LLMIncompleteError(reason, billed_usage)
    text = response.output_text
    if not text.strip():
        reason = "empty_output"
        raise LLMIncompleteError(reason, billed_usage)
    return BackendReply(
        text=text,
        model=response.model,
        response_id=response.id,
        usage=usage,
        usage_reported=response.usage is not None,
    )


def _usage_from(response: Response, fallback: TokenUsage) -> TokenUsage:
    usage = response.usage
    if usage is None:
        return fallback
    return TokenUsage(
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cached_input_tokens=usage.input_tokens_details.cached_tokens,
        cache_write_tokens=usage.input_tokens_details.cache_write_tokens,
        reasoning_tokens=usage.output_tokens_details.reasoning_tokens,
    )
