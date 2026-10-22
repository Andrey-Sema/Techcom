"""Bound request bytes before parsing, including chunked input."""

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

MAX_REQUEST_BYTES = 16_384


class BodyLimitMiddleware:
    """Buffer at most one small mock payload; trust no length header."""

    def __init__(self, app: ASGIApp) -> None:
        """Wrap an application with a fixed request byte budget."""
        self._app = app

    async def __call__(
        self, scope: Scope, receive: Receive, send: Send
    ) -> None:
        """Return 413 before validation if the request is too large."""
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        chunks = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body = message.get("body", b"")
            if len(chunks) + len(body) > MAX_REQUEST_BYTES:
                response = JSONResponse(
                    {"detail": "request too large"}, status_code=413
                )
                await response(scope, receive, send)
                return
            chunks.extend(body)
            if not message.get("more_body", False):
                break

        consumed = False

        async def buffered_receive() -> Message:
            nonlocal consumed
            if consumed:
                return await receive()
            consumed = True
            return {"type": "http.request", "body": bytes(chunks)}

        await self._app(scope, buffered_receive, send)
