"""Shared bounded, fail-closed execution of language-only requests.

No retries or tools live here. Audit events contain closed failure codes
and a digest, never provider errors or untrusted input/output text.
"""

import time
from collections import Counter
from collections.abc import Callable
from hashlib import sha256

import structlog
from pydantic import BaseModel, ConfigDict, ValidationError

from techcom_followup.application.ports import LLMClient, LLMError, LLMRequest

MAX_OUTPUT_CHARS = 16_384
BREAKER_COOLDOWN_S = 30
BREAKER_FAILURE_THRESHOLD = 5
_log = structlog.get_logger(__name__)


class StrictOutput(BaseModel):
    """Closed local schema; provider enforcement is never trusted."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)


class InvalidOutputError(Exception):
    """A closed validation stage, safe to include in audit metadata."""


class LanguageCall:
    """One-attempt language calls with a local availability breaker.

    Synchronous admission and settlement run on one event loop. An open
    breaker admits one probe after thirty seconds. Cancellation releases
    the probe and propagates to the caller.
    """

    def __init__(
        self,
        client: LLMClient | None,
        *,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        """Use an optional client; absent clients always fall back."""
        self.client = client
        self.failures: Counter[str] = Counter()
        self._monotonic = monotonic
        self._consecutive = 0
        self._opened_at: float | None = None
        self._probe = False

    def _allow(self) -> bool:
        if self._opened_at is None:
            return True
        if (
            self._probe
            or self._monotonic() - self._opened_at < BREAKER_COOLDOWN_S
        ):
            return False
        self._probe = True
        return True

    def reject(self, stage: str, request: LLMRequest, text: str = "") -> None:
        """Count a validation failure without exposing its contents."""
        self.failures[stage] += 1
        self._consecutive += 1
        if self._consecutive >= BREAKER_FAILURE_THRESHOLD:
            self._opened_at = self._monotonic()
        _log.warning(
            "llm.validation_failed",
            stage=stage,
            prompt_version=request.prompt_version,
            output_sha256=sha256(
                text.encode("utf-8", "surrogatepass")
            ).hexdigest(),
        )

    async def call(self, request: LLMRequest) -> str | None:
        """Return text or a safe failure; propagate cancellation."""
        if self.client is None or not self._allow():
            self.failures["unavailable"] += 1
            return None
        try:
            result = await self.client.generate_json(request)
        except (LLMError, TimeoutError):
            self.reject("transport", request)
            return None
        finally:
            self._probe = False
        return result.text

    def accept(self) -> None:
        """Close the breaker only after local validation succeeds."""
        self._consecutive = 0
        self._opened_at = None


def parse_output[Output: StrictOutput](
    text: str, schema: type[Output]
) -> Output:
    """Validate bounded JSON using strict JSON-mode enum semantics.

    Raises:
        InvalidOutputError: A closed syntax or schema failure code.
    """
    if len(text) > MAX_OUTPUT_CHARS:
        message = "size"
        raise InvalidOutputError(message)
    try:
        return schema.model_validate_json(text)
    except ValidationError as exc:
        stage = (
            "json"
            if any(error["type"] == "json_invalid" for error in exc.errors())
            else "schema"
        )
        raise InvalidOutputError(stage) from None


def require_output(condition: object, stage: str) -> None:
    """Raise a closed failure code when an output invariant fails.

    Raises:
        InvalidOutputError: The invariant did not hold.
    """
    if not condition:
        raise InvalidOutputError(stage)
