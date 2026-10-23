"""Bounded, fail-closed execution of language-only requests.

Validation settles the same attempt that obtained admission. Stale
requests cannot release another request's half-open probe or reset its
breaker generation. Audit records omit provider errors and raw text.
"""

import asyncio
import time
from collections import Counter
from collections.abc import Callable
from hashlib import sha256
from math import isfinite

import structlog
from pydantic import BaseModel, ConfigDict, ValidationError

from techcom_followup.application.ports import LLMClient, LLMError, LLMRequest
from techcom_followup.application.resilience_ports import BreakerPermit
from techcom_followup.application.telemetry import (
    NULL_TELEMETRY,
    CounterName,
    Telemetry,
)

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
    """One-attempt calls with an owned breaker and total deadline."""

    def __init__(
        self,
        client: LLMClient | None,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        timeout_s: float = 10,
        telemetry: Telemetry = NULL_TELEMETRY,
    ) -> None:
        """Inject a language port and a finite total call deadline.

        ``telemetry`` is public so a composition root can attach it to
        workflows that own their language call.
        """
        if not isfinite(timeout_s) or timeout_s <= 0:
            message = "language timeout must be finite and positive"
            raise ValueError(message)
        self.client = client
        self.telemetry = telemetry
        self.failures: Counter[str] = Counter()
        self._monotonic = monotonic
        self._timeout_s = timeout_s
        self._consecutive = 0
        self._opened_at: float | None = None
        self._probe: BreakerPermit | None = None
        self._generation = 0
        self._identity = object()

    def _admit(self) -> BreakerPermit | None:
        if self._opened_at is not None and (
            self._probe is not None
            or self._monotonic() - self._opened_at < BREAKER_COOLDOWN_S
        ):
            return None
        permit = BreakerPermit(self._identity, self._generation)
        if self._opened_at is not None:
            self._probe = permit
        return permit

    def _reject(
        self, stage: str, request: LLMRequest, text: str, permit: BreakerPermit
    ) -> None:
        self.failures[stage] += 1
        self._count(request, "error" if stage == "transport" else "invalid")
        if permit.generation == self._generation:
            self._consecutive += 1
            if self._consecutive >= BREAKER_FAILURE_THRESHOLD:
                self._opened_at = self._monotonic()
                self._generation += 1
                self._probe = None
        _log.warning(
            "llm.validation_failed",
            stage=stage,
            prompt_version=request.prompt_version,
            output_sha256=sha256(
                text.encode("utf-8", "surrogatepass")
            ).hexdigest(),
        )

    async def call[Output](
        self, request: LLMRequest, validate: Callable[[str], Output]
    ) -> Output | None:
        """Validate locally and propagate external cancellation."""
        permit = self._admit()
        if self.client is None or permit is None:
            self.failures["unavailable"] += 1
            self._count(request, "unavailable")
            return None
        text = ""
        try:
            async with asyncio.timeout(self._timeout_s):
                result = await self.client.generate_json(request)
            text = result.text
            output = validate(text)
        except InvalidOutputError as exc:
            self._reject(str(exc), request, text, permit)
        except (LLMError, TimeoutError):
            self._reject("transport", request, "", permit)
        else:
            self._accept(permit)
            self._count(request, "ok")
            return output
        finally:
            if self._probe is permit:
                self._probe = None
        return None

    def _count(self, request: LLMRequest, result: str) -> None:
        self.telemetry.count(
            CounterName.LLM_REQUESTS,
            purpose=request.purpose.value,
            result=result,
        )

    def _accept(self, permit: BreakerPermit) -> None:
        if permit.generation == self._generation:
            if self._opened_at is not None:
                self._generation += 1
            self._consecutive = 0
            self._opened_at = None
            self._probe = None


def parse_output[Output: StrictOutput](
    text: str, schema: type[Output]
) -> Output:
    """Validate bounded JSON with strict JSON-mode enum semantics.

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
