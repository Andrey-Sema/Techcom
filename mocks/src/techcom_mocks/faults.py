"""Deterministic endpoint fault plans and a hermetic HTTP transport."""

from collections import Counter
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Self, override

import httpx


class Fault(StrEnum):
    """Faults exposed only by local test infrastructure."""

    RATE_LIMIT = "429"
    INTERNAL_ERROR = "500"
    UNAVAILABLE = "503"
    TIMEOUT_BEFORE_COMMIT = "timeout_before_commit"
    TIMEOUT_AFTER_COMMIT = "timeout_after_commit"
    MALFORMED = "malformed"
    RESET = "reset"
    NON_IDEMPOTENT = "non_idempotent"


@dataclass(slots=True)
class FaultPlan:
    """Map endpoint call numbers to faults without clocks or sleeps."""

    entries: dict[tuple[str, int], Fault] = field(default_factory=dict)
    _calls: Counter[str] = field(default_factory=Counter, init=False)

    @classmethod
    def parse(cls, value: str) -> Self:
        """Parse ``tasks:429@1,messages:timeout_after_commit@1``.

        Duplicates, unknown endpoints/faults and non-positive call
        numbers fail configuration, rather than silently doing nothing.
        """
        entries: dict[tuple[str, int], Fault] = {}
        for item in filter(None, value.split(",")):
            endpoint, fault_at = item.strip().split(":", 1)
            fault_name, raw_call = fault_at.split("@", 1)
            call = int(raw_call)
            key = (endpoint, call)
            if endpoint not in {"tasks", "messages", "lookup"} or call < 1:
                msg = "invalid fault endpoint or call number"
                raise ValueError(msg)
            if key in entries:
                msg = "duplicate fault for endpoint call"
                raise ValueError(msg)
            entries[key] = Fault(fault_name)
        return cls(entries)

    def next_fault(self, endpoint: str) -> Fault | None:
        """Consume exactly one endpoint call."""
        self._calls[endpoint] += 1
        return self.entries.get((endpoint, self._calls[endpoint]))


def response_for(fault: Fault | None) -> httpx.Response | None:
    """Build faults that can be faithfully represented by HTTP."""
    if fault in {Fault.RATE_LIMIT, Fault.INTERNAL_ERROR, Fault.UNAVAILABLE}:
        return httpx.Response(
            int(fault), headers={"Retry-After": "1"}, json={"detail": fault}
        )
    if fault is Fault.MALFORMED:
        return httpx.Response(200, content=b"this is not JSON")
    return None


class FaultInjectingTransport(httpx.AsyncBaseTransport):
    """Wrap ASGI/network transports to model lost acknowledgements.

    ASGITransport does not enforce HTTP timeouts. Raising ReadTimeout
    after forwarding makes timeout-after-commit tests exact and fast.
    """

    def __init__(
        self, wrapped: httpx.AsyncBaseTransport, plan: FaultPlan
    ) -> None:
        """Own the wrapped transport and an isolated fault counter."""
        self._wrapped = wrapped
        self._plan = plan

    @override
    async def handle_async_request(
        self, request: httpx.Request
    ) -> httpx.Response:
        """Inject before forwarding or discard a committed response."""
        path = request.url.path
        if path == "/v1/tasks":
            endpoint = "tasks"
        elif path == "/v1/messages" and request.method == "POST":
            endpoint = "messages"
        else:
            endpoint = "lookup"
        fault = self._plan.next_fault(endpoint)
        if fault is Fault.TIMEOUT_BEFORE_COMMIT:
            msg = "injected timeout before commit"
            raise httpx.ReadTimeout(msg, request=request)
        if fault is Fault.RESET:
            msg = "injected connection reset"
            raise httpx.ReadError(msg, request=request)
        injected = response_for(fault)
        if injected is not None:
            return injected
        if fault is Fault.NON_IDEMPOTENT:
            msg = "non_idempotent must be configured on the receiver"
            raise ValueError(msg)
        response = await self._wrapped.handle_async_request(request)
        if fault is Fault.TIMEOUT_AFTER_COMMIT:
            await response.aclose()
            msg = "injected timeout after commit"
            raise httpx.ReadTimeout(msg, request=request)
        return response

    @override
    async def aclose(self) -> None:
        """Close the wrapped transport."""
        await self._wrapped.aclose()
