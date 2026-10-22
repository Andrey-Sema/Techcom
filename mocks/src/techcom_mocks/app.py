"""Factory for a loopback-only, single-worker development receiver.

Do not expose this mock to production or an untrusted network. Service
JWTs and project authorization belong to the production API (M6b).
"""

import asyncio
import json
import os
from pathlib import Path
from typing import Annotated, Any
from uuid import UUID

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response

from techcom_mocks.faults import Fault, FaultPlan, response_for
from techcom_mocks.limits import BodyLimitMiddleware
from techcom_mocks.models import DeliveryPayload, ErrorBody, Receipt
from techcom_mocks.store import (
    DeliveryStore,
    InProgressError,
    PayloadMismatchError,
    StoreUnavailableError,
)

_INJECTED = "Injected by FOLLOWUP_MOCK_FAULTS"


def _errors(codes: dict[int, str]) -> dict[int | str, dict[str, Any]]:
    """Document error responses so the OpenAPI matches the wire."""
    documented = dict.fromkeys((429, 500, 503, 504), _INJECTED) | codes
    return {
        code: {"model": ErrorBody, "description": text}
        for code, text in documented.items()
    }


_INVALID = "Invalid request; the input is never echoed"
_IN_PROGRESS = "Operation in progress; retry after Retry-After"


async def _timeout() -> Response:
    # Real clients time out; hermetic tests use the fault transport.
    await asyncio.sleep(30)
    return JSONResponse({"detail": "injected timeout"}, status_code=504)


async def _apply_fault(fault: Fault | None) -> Response | None:
    if fault in {Fault.TIMEOUT_BEFORE_COMMIT, Fault.TIMEOUT_AFTER_COMMIT}:
        return await _timeout()
    if fault is Fault.RESET:
        # TCP resets require FaultInjectingTransport.
        return JSONResponse({"detail": "use fault transport"}, status_code=503)
    injected = response_for(fault)
    if injected is not None:
        return Response(
            injected.content,
            status_code=injected.status_code,
            headers=dict(injected.headers),
        )
    return None


async def _validation_error(
    _request: Request, _error: Exception
) -> JSONResponse:
    # FastAPI's default handler echoes invalid input, including notes.
    return JSONResponse({"detail": "invalid request"}, status_code=422)


async def _unavailable(_request: Request, _error: Exception) -> JSONResponse:
    return JSONResponse({"detail": "journal unavailable"}, status_code=503)


class Receiver:
    """Local endpoints with explicitly injected dependencies."""

    def __init__(
        self, tasks: list[object], store: DeliveryStore, faults: FaultPlan
    ) -> None:
        """Keep one receiver state for the lifespan of the mock app."""
        self._tasks = tasks
        self._store = store
        self._faults = faults

    async def get_tasks(self) -> Response:
        """Return source fixture records without mutating them."""
        injected = await _apply_fault(self._faults.next_fault("tasks"))
        return JSONResponse(self._tasks) if injected is None else injected

    async def deliver(
        self,
        payload: DeliveryPayload,
        idempotency_key: Annotated[UUID, Header(alias="Idempotency-Key")],
    ) -> Response:
        """Commit a message or replay its original acknowledgement."""
        fault = self._faults.next_fault("messages")
        if fault is not Fault.TIMEOUT_AFTER_COMMIT:
            injected = await _apply_fault(fault)
            if injected is not None:
                return injected
        try:
            receipt, replayed = await self._store.deliver(
                idempotency_key,
                payload,
                idempotent=fault is not Fault.NON_IDEMPOTENT,
            )
        except PayloadMismatchError as exc:
            raise HTTPException(422, "idempotency payload mismatch") from exc
        except InProgressError as exc:
            raise HTTPException(
                409, "operation in progress", headers={"Retry-After": "1"}
            ) from exc
        if fault is Fault.TIMEOUT_AFTER_COMMIT:
            return await _timeout()
        return JSONResponse(
            receipt.model_dump(mode="json"),
            status_code=201,
            headers={"Idempotent-Replayed": str(replayed).lower()},
        )

    async def lookup(self, operation_id: UUID) -> Response:
        """Support reconciliation without sending another message."""
        injected = await _apply_fault(self._faults.next_fault("lookup"))
        if injected is not None:
            return injected
        try:
            receipt = await self._store.lookup(operation_id)
        except InProgressError as exc:
            raise HTTPException(
                409, "operation in progress", headers={"Retry-After": "1"}
            ) from exc
        if receipt is None:
            raise HTTPException(404, "operation not found")
        return JSONResponse(receipt.model_dump(mode="json"))


def create_app(
    *,
    tasks: list[object] | None = None,
    store: DeliveryStore | None = None,
    faults: FaultPlan | None = None,
) -> FastAPI:
    """Configure mocks from operator settings or injected state.

    Paths and fault plans are never taken from request parameters.
    """
    if tasks is None:
        fixture = Path(
            os.environ.get("FOLLOWUP_MOCK_FIXTURE", "fixtures/tasks.json")
        )
        tasks = json.loads(fixture.read_text(encoding="utf-8"))
    records = json.loads(json.dumps(tasks))
    if store is None:
        journal = os.environ.get("FOLLOWUP_MOCK_JOURNAL")
        store = DeliveryStore(None if journal is None else Path(journal))
    if faults is None:
        faults = FaultPlan.parse(os.environ.get("FOLLOWUP_MOCK_FAULTS", ""))
    receiver = Receiver(records, store, faults)
    app = FastAPI(title="TechCom local mock", docs_url=None, redoc_url=None)
    app.add_middleware(BodyLimitMiddleware)
    app.add_exception_handler(RequestValidationError, _validation_error)
    app.add_exception_handler(StoreUnavailableError, _unavailable)
    app.add_api_route(
        "/v1/tasks",
        receiver.get_tasks,
        methods=["GET"],
        response_model=list[dict[str, Any]],
        responses=_errors({}),
    )
    app.add_api_route(
        "/v1/messages",
        receiver.deliver,
        methods=["POST"],
        status_code=201,
        response_model=Receipt,
        responses=_errors(
            {
                400: "Body is not valid JSON",
                409: _IN_PROGRESS,
                413: "Request body too large",
                422: _INVALID + ", or the key was used with another payload",
                503: "Journal unavailable, or an injected fault",
            }
        ),
    )
    app.add_api_route(
        "/v1/messages/{operation_id}",
        receiver.lookup,
        methods=["GET"],
        response_model=Receipt,
        responses=_errors(
            {404: "Unknown operation", 409: _IN_PROGRESS, 422: _INVALID}
        ),
    )
    return app
