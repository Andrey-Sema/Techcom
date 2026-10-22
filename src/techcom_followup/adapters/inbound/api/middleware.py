"""HTTP limits, request correlation and security headers."""

from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from http import HTTPStatus
from math import isfinite
from urllib.parse import parse_qsl
from uuid import UUID, uuid4

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send
from structlog.contextvars import bind_contextvars, reset_contextvars

from techcom_followup.adapters.inbound.api.observability import Observer
from techcom_followup.adapters.inbound.api.ports import SecurityEvent

MAX_REQUEST_BYTES = 65_536
MAX_QUERY_BYTES = 4096
MAX_QUERY_FIELDS = 16


@dataclass(frozen=True, slots=True)
class ApiSettings:
    """Production disables interactive surfaces and adds HSTS."""

    production: bool = True
    rate_capacity: int = 60
    rate_refill_s: float = 60
    max_rate_subjects: int = 1024

    def __post_init__(self) -> None:
        """Keep local rate guards bounded and nonzero."""
        if (
            self.rate_capacity < 1
            or self.rate_refill_s <= 0
            or not isfinite(self.rate_refill_s)
            or self.max_rate_subjects < 1
        ):
            message = "rate limiting requires positive bounded settings"
            raise ValueError(message)


def security_headers(request_id: UUID, *, production: bool) -> dict[str, str]:
    """Use one set of headers for success and every error boundary."""
    headers = {
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
        "Content-Security-Policy": (
            "default-src 'none'; frame-ancestors 'none'"
        ),
        "X-Request-ID": str(request_id),
    }
    if production:
        headers["Strict-Transport-Security"] = (
            "max-age=31536000; includeSubDomains"
        )
    return headers


def problem(
    status: int, request_id: UUID, *, headers: Mapping[str, str] | None = None
) -> JSONResponse:
    """Return a generic RFC 9457 error without reflecting user input."""
    return JSONResponse(
        {
            "type": "about:blank",
            "title": HTTPStatus(status).phrase,
            "status": status,
            "request_id": str(request_id),
        },
        status_code=status,
        media_type="application/problem+json",
        headers=headers,
    )


def _request_id(scope: Scope) -> UUID:
    values = [
        value
        for key, value in scope.get("headers", ())
        if key.lower() == b"x-request-id"
    ]
    if len(values) == 1:
        try:
            return UUID(values[0].decode("ascii"))
        except (ValueError, UnicodeError):
            pass
    return uuid4()


def _query_error(scope: Scope) -> int | None:
    raw = scope.get("query_string", b"")
    if len(raw) > MAX_QUERY_BYTES:
        return 414
    try:
        pairs = parse_qsl(
            raw.decode("ascii"),
            keep_blank_values=True,
            errors="strict",
            max_num_fields=MAX_QUERY_FIELDS,
        )
    except (ValueError, UnicodeError):
        return 400
    names = [key for key, _value in pairs]
    return 400 if len(set(names)) != len(names) else None


@dataclass(slots=True)
class _ResponseState:
    started: bool = False
    finished: bool = False


class SecurityMiddleware:
    """Bound request bytes and attach security headers."""

    def __init__(
        self, app: ASGIApp, settings: ApiSettings, observer: Observer
    ) -> None:
        """Inject settings and an isolated observer instance."""
        self._app, self._settings, self._observer = app, settings, observer

    async def __call__(
        self, scope: Scope, receive: Receive, send: Send
    ) -> None:
        """Bind safe correlation for the request lifetime."""
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        request_id = _request_id(scope)
        scope.setdefault("state", {})["request_id"] = request_id
        tokens = bind_contextvars(request_id=str(request_id))
        try:
            await self._http(scope, receive, send, request_id)
        finally:
            reset_contextvars(**tokens)

    async def _http(
        self, scope: Scope, receive: Receive, send: Send, request_id: UUID
    ) -> None:
        headers = security_headers(
            request_id, production=self._settings.production
        )

        state = _ResponseState()

        secure_send = self._secured_send(send, headers, state)

        query_error = _query_error(scope)
        if query_error is not None:
            await problem(query_error, request_id)(scope, receive, secure_send)
            return
        body = await self._body(scope, receive, secure_send, request_id)
        if body is None:
            return
        consumed = False

        async def buffered_receive() -> Message:
            nonlocal consumed
            if consumed:
                return await receive()
            consumed = True
            return {"type": "http.request", "body": body}

        completed = False
        # The outer ASGI server logs uncaught exception messages. Keep
        # dependency errors inside this HTTP boundary, with no raw text.
        # CancelledError and all other BaseExceptions still propagate.
        with suppress(Exception):
            await self._app(scope, buffered_receive, secure_send)
            completed = True
        if not completed:
            await self._failed_response(
                scope, receive, secure_send, request_id, state
            )

    def _secured_send(
        self, send: Send, headers: Mapping[str, str], state: _ResponseState
    ) -> Send:
        """Track protocol progress and apply security headers."""

        async def secure_send(message: Message) -> None:
            if message["type"] == "http.response.start":
                state.started = True
                existing = list(message.get("headers", ()))
                names = {name.lower().encode() for name in headers}
                message["headers"] = [
                    (key, value)
                    for key, value in existing
                    if key.lower() not in names
                ] + [
                    (key.lower().encode(), value.encode())
                    for key, value in headers.items()
                ]
                self._observer.response(message["status"])
            if message["type"] == "http.response.body":
                state.finished = not message.get("more_body", False)
            await send(message)

        return secure_send

    async def _failed_response(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
        request_id: UUID,
        state: _ResponseState,
    ) -> None:
        status = 503 if scope.get("path") == "/readyz" else 500
        self._observer.emit(
            SecurityEvent("api.unhandled_error", request_id, status)
        )
        if not state.started:
            await problem(status, request_id)(scope, receive, send)
        elif not state.finished:
            await send({"type": "http.response.body", "body": b""})

    @staticmethod
    async def _body(
        scope: Scope, receive: Receive, send: Send, request_id: UUID
    ) -> bytes | None:
        chunks = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return None
            body = message.get("body", b"")
            if len(chunks) + len(body) > MAX_REQUEST_BYTES:
                await problem(413, request_id)(scope, receive, send)
                return None
            chunks.extend(body)
            if not message.get("more_body", False):
                return bytes(chunks)
