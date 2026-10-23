from collections.abc import AsyncIterator

import httpx
from starlette.types import Message, Receive, Scope, Send

from techcom_mocks.app import create_app
from techcom_mocks.limits import MAX_REQUEST_BYTES, BodyLimitMiddleware


async def test_chunked_body_limit_ignores_claimed_content_length() -> None:
    async def chunks() -> AsyncIterator[bytes]:
        yield b"x" * (MAX_REQUEST_BYTES // 2)
        yield b"x" * (MAX_REQUEST_BYTES // 2 + 1)

    app = create_app(tasks=[])
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://mock"
    ) as client:
        response = await client.post(
            "/v1/messages", headers={"Content-Length": "1"}, content=chunks()
        )
    assert response.status_code == 413
    assert response.json() == {"detail": "request too large"}


async def test_limit_boundary_still_reaches_json_validation() -> None:
    app = create_app(tasks=[])
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://mock"
    ) as client:
        response = await client.post(
            "/v1/messages", content=b"x" * MAX_REQUEST_BYTES
        )
    assert response.status_code == 422


async def test_non_http_scope_passes_through_and_disconnect_stops() -> None:
    calls: list[Scope] = []
    messages: list[Message] = []

    async def wrapped(scope: Scope, receive: Receive, send: Send) -> None:
        calls.append(scope)

    async def receive() -> Message:
        return {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        messages.append(message)

    middleware = BodyLimitMiddleware(wrapped)
    await middleware({"type": "lifespan"}, receive, send)
    assert calls == [{"type": "lifespan"}]
    await middleware({"type": "http"}, receive, send)
    assert len(calls) == 1
    assert messages == []


async def test_buffer_is_delivered_once_then_disconnect_is_forwarded() -> None:
    incoming: list[Message] = [
        {"type": "http.request", "body": b"first", "more_body": True},
        {"type": "http.request", "body": b"second"},
        {"type": "http.disconnect"},
    ]
    observed: list[Message] = []

    async def receive() -> Message:
        return incoming.pop(0)

    async def send(message: Message) -> None:
        observed.append(message)

    async def wrapped(scope: Scope, receive: Receive, send: Send) -> None:
        observed.append(await receive())
        observed.append(await receive())

    await BodyLimitMiddleware(wrapped)({"type": "http"}, receive, send)
    assert observed == [
        {"type": "http.request", "body": b"firstsecond"},
        {"type": "http.disconnect"},
    ]
