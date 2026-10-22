"""Ports (interfaces) the application layer depends on.

Only the LLM port lives here for now; task source, delivery and
repository ports are added by their milestones.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Protocol


class LLMPurpose(StrEnum):
    """Why the application calls a language model.

    The router keeps a separate, ordered list of model routes per
    purpose so that cost and quality can be tuned independently.
    """

    INTERPRET_NOTE = "interpret_note"
    DRAFT_MESSAGE = "draft_message"


@dataclass(frozen=True, slots=True)
class LLMRequest:
    """A provider-neutral request for schema-constrained JSON output.

    Attributes:
        purpose: Selects the route list in the router.
        instructions: Trusted system instructions (never task data).
        input_text: Untrusted content, already delimited and escaped by
            the caller.
        schema_name: Name of the JSON schema sent to the provider.
        json_schema: Strict JSON schema the output must follow. The
            provider-side schema is a convenience; the caller validates
            the returned text again with its own strict model.
        prompt_version: Version tag of the prompt; used for prompt
            caching and audit.
        safety_identifier: Stable, non-personal identifier of the end
            consumer (for example a hashed service account), if any.
    """

    purpose: LLMPurpose
    instructions: str
    input_text: str
    schema_name: str
    json_schema: Mapping[str, object]
    prompt_version: str
    safety_identifier: str | None = None


@dataclass(frozen=True, slots=True)
class TokenUsage:
    """Token accounting reported by the provider for one response."""

    input_tokens: int
    output_tokens: int
    cached_input_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0


@dataclass(frozen=True, slots=True)
class RouteAttempt:
    """Outcome of trying one model route, kept for audit and metrics."""

    provider: str
    model: str
    outcome: str


@dataclass(frozen=True, slots=True)
class LLMResult:
    """Raw provider output plus metadata for cost and audit."""

    text: str
    provider: str
    model: str
    response_id: str
    usage: TokenUsage
    cost_usd: Decimal
    latency_s: float
    attempts: tuple[RouteAttempt, ...] = field(default=())


class LLMError(Exception):
    """Base class for every LLM failure the application must handle."""


class LLMBilledError(LLMError):
    """A failure that still consumed (and billed) tokens."""

    def __init__(self, message: str, usage: TokenUsage | None) -> None:
        """Keep the usage so the caller can still account the cost."""
        super().__init__(message)
        self.usage = usage


class LLMUnavailableError(LLMError):
    """No configured route produced a response."""

    def __init__(self, attempts: tuple[RouteAttempt, ...]) -> None:
        """Store the attempts so the caller can audit the failover."""
        super().__init__("no LLM route produced a response")
        self.attempts = attempts


class LLMRefusalError(LLMBilledError):
    """The model refused to answer."""


class LLMIncompleteError(LLMBilledError):
    """The response was cut short (token limit, content filter, ...)."""

    def __init__(self, reason: str, usage: TokenUsage | None) -> None:
        """Keep the provider's reason (a closed, non-sensitive enum)."""
        super().__init__(f"incomplete LLM response: {reason}", usage)
        self.reason = reason


class LLMBudgetExceededError(LLMError):
    """The daily LLM spending budget is exhausted."""


class LLMConfigurationError(LLMError):
    """Credentials or configuration are wrong; retrying cannot help."""


class LLMClient(Protocol):
    """Port: produce schema-constrained JSON text for a request."""

    async def generate_json(self, request: LLMRequest) -> LLMResult:
        """Return the raw JSON text produced by a model.

        Raises:
            LLMError: Any failure; callers must fall back to their
                deterministic path.
        """
        ...
