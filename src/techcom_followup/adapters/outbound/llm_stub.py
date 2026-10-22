"""Scripted offline language port for raw JSON and failures."""

from collections import deque
from collections.abc import Iterable, Mapping
from decimal import Decimal
from hashlib import sha256

from techcom_followup.application.ports import (
    LLMError,
    LLMRequest,
    LLMResult,
    TokenUsage,
)


def request_key(request: LLMRequest) -> str:
    """Key scripts by purpose, prompt version and isolated data."""
    raw = f"{request.purpose}\0{request.prompt_version}\0{request.input_text}"
    return sha256(raw.encode("utf-8", "surrogatepass")).hexdigest()


class ScriptedLLM:
    """Serve finite scripts with no network, sleeps or implicit retries.

    A per-key script takes precedence over the general FIFO script.
    Exhaustion raises a closed LLM error; malformed JSON is returned
    unchanged so the application's local validation is exercised.
    """

    def __init__(
        self,
        outcomes: Iterable[str | LLMError | TimeoutError] = (),
        *,
        scripts: Mapping[str, Iterable[str | LLMError | TimeoutError]] = {},
    ) -> None:
        """Copy the script and record calls for explicit assertions."""
        self._outcomes = deque(outcomes)
        self._scripts = {key: deque(values) for key, values in scripts.items()}
        self.requests: list[LLMRequest] = []

    async def generate_json(self, request: LLMRequest) -> LLMResult:
        """Return or raise exactly one scripted outcome.

        Raises:
            LLMError: The script is exhausted or requests a failure.
            TimeoutError: The script models a transport timeout.
        """
        self.requests.append(request)
        script = self._scripts.get(request_key(request), self._outcomes)
        if not script:
            message = "script exhausted"
            raise LLMError(message)
        outcome = script.popleft()
        if isinstance(outcome, Exception):
            raise outcome
        return LLMResult(
            text=outcome,
            provider="stub",
            model="stub.v1",
            response_id=f"stub-{len(self.requests)}",
            usage=TokenUsage(0, 0),
            cost_usd=Decimal(0),
            latency_s=0,
        )
