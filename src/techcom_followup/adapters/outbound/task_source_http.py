"""Idempotent task reads with one bounded stamina retry layer."""

import asyncio
from collections.abc import Callable
from random import SystemRandom

import httpx
import stamina

from techcom_followup.application.processing_ports import Clock
from techcom_followup.application.retry import RetryBudget, RetryPolicy
from techcom_followup.infrastructure.http_client import (
    HttpEndpoint,
    UpstreamError,
    json_body,
    retry_after,
)

_RANDOM = SystemRandom()
MAX_TASKS = 10_000
MAX_ATTEMPTS = 4
_RETRYABLE_CODES = frozenset({429, 500, 502, 503, 504})


class _TransientReadError(UpstreamError):
    def __init__(self, delay: float) -> None:
        super().__init__("source_transient")
        self.delay = delay


class HttpTaskSource:
    """Never retry permanent errors or invalid envelopes."""

    def __init__(
        self,
        endpoint: HttpEndpoint,
        clock: Clock,
        *,
        budget: RetryBudget,
        jitter: Callable[[], float] = _RANDOM.random,
    ) -> None:
        """Share a retry budget for the entire source read cycle."""
        self._endpoint = endpoint
        self._clock = clock
        self._budget = budget
        self._jitter = jitter

    async def fetch_tasks(self) -> tuple[object, ...]:
        """Fetch at most 10,000 records within bounded retries."""
        policy = RetryPolicy(max_attempts=4, base_seconds=0.5, cap_seconds=10)
        count = 0

        def backoff(error: Exception) -> bool | float:
            if (
                not isinstance(error, _TransientReadError)
                or count >= MAX_ATTEMPTS
            ):
                return False
            if not self._budget.take():
                return False
            return policy.delay(
                count, jitter=self._jitter(), retry_after=error.delay
            )

        async def read() -> tuple[object, ...]:
            nonlocal count
            count += 1
            return await self._fetch()

        retrying_read = stamina.retry(
            on=backoff, attempts=MAX_ATTEMPTS, timeout=20
        )(read)
        async with asyncio.timeout(20):
            return await retrying_read()

    async def _fetch(self) -> tuple[object, ...]:
        try:
            response = await self._endpoint.request("GET", "/v1/tasks")
        except (
            httpx.TimeoutException,
            httpx.NetworkError,
            TimeoutError,
        ) as error:
            raise _TransientReadError(0) from error
        if response.status_code in _RETRYABLE_CODES:
            raise _TransientReadError(
                min(
                    10,
                    retry_after(
                        response.headers.get("Retry-After"), self._clock.now()
                    ),
                )
            )
        if response.status_code != httpx.codes.OK:
            msg = "source_http_status"
            raise UpstreamError(msg)
        body = json_body(response)
        if not isinstance(body, list) or len(body) > MAX_TASKS:
            msg = "source_shape"
            raise UpstreamError(msg)
        return tuple(body)
