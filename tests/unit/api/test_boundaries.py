from collections.abc import Callable
from uuid import UUID, uuid4

import httpx
import pytest
from prometheus_client import generate_latest
from starlette.types import Message, Receive, Scope, Send

from api_support import NOW, ApiClock, Harness, RecordingSink
from techcom_followup.adapters.inbound.api.middleware import (
    ApiSettings,
    SecurityMiddleware,
)
from techcom_followup.adapters.inbound.api.models import TriggerRequest
from techcom_followup.adapters.inbound.api.observability import Observer
from techcom_followup.adapters.inbound.api.ports import SecurityEvent
from techcom_followup.adapters.inbound.api.rate_limit import RateLimiter


@pytest.mark.parametrize(
    "invalid",
    [
        lambda: ApiSettings(rate_capacity=0),
        lambda: ApiSettings(rate_refill_s=0),
        lambda: ApiSettings(max_rate_subjects=0),
        lambda: ApiSettings(rate_refill_s=float("inf")),
        lambda: ApiSettings(rate_refill_s=float("nan")),
    ],
)
def test_invalid_rate_settings_fail_startup(
    invalid: Callable[[], ApiSettings],
) -> None:
    with pytest.raises(ValueError, match="positive"):
        invalid()


def test_rate_bucket_storage_is_bounded_and_idle_entries_expire() -> None:
    clock = ApiClock()
    limiter = RateLimiter(
        ApiSettings(rate_capacity=2, max_rate_subjects=1), clock.monotonic
    )
    assert limiter.allow("A")
    assert not limiter.allow("B")
    assert limiter.allow("A")
    assert not limiter.allow("A")
    clock.advance(30)
    assert limiter.allow("A")
    clock.advance(60)
    assert limiter.allow("B")


@pytest.mark.asvs("16.5.3")
def test_exporter_failure_is_counted_without_breaking_the_request() -> None:
    class BrokenSink:
        def emit(self, event: SecurityEvent) -> None:
            message = "OTLP unavailable"
            raise OSError(message)

    observer = Observer(BrokenSink())
    observer.emit(SecurityEvent("security.authn_failed", uuid4(), 401))
    metrics = generate_latest(observer.registry).decode()
    assert "followup_api_logging_errors_total 1.0" in metrics


@pytest.mark.asvs("9.1.1", "16.2.5")
async def test_malformed_and_duplicate_authorization_are_generic() -> None:
    harness = Harness()
    async with harness.client() as client:
        for headers in (
            {"Authorization": "Bearer invalid-secret-token"},
            {"Authorization": "Basic invalid-secret-token"},
            [
                ("Authorization", "Bearer " + harness.token()),
                ("Authorization", "Bearer " + harness.token()),
            ],
        ):
            response = await client.get("/v1/actions", headers=headers)
            assert response.status_code == 401
            assert "secret-token" not in response.text
    assert "invalid-secret-token" not in str(harness.sink.events)


async def test_non_http_scope_and_disconnect_are_handled() -> None:
    calls: list[Scope] = []

    async def wrapped(scope: Scope, receive: Receive, send: Send) -> None:
        calls.append(scope)

    async def receive() -> Message:
        return {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        pytest.fail(f"disconnected request must not receive {message['type']}")

    middleware = SecurityMiddleware(wrapped, ApiSettings(), Observer())
    await middleware({"type": "lifespan"}, receive, send)
    assert calls == [{"type": "lifespan"}]
    await middleware({"type": "http"}, receive, send)
    assert len(calls) == 1


async def test_buffer_is_delivered_once_and_disconnect_is_forwarded() -> None:
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
        assert await receive() == {
            "type": "http.request",
            "body": b"firstsecond",
        }
        assert await receive() == {"type": "http.disconnect"}
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"cache-control", b"public")],
            }
        )
        await send({"type": "http.response.body", "body": b"ok"})

    await SecurityMiddleware(wrapped, ApiSettings(), Observer())(
        {"type": "http", "headers": [(b"x-request-id", b"\xff")]},
        receive,
        send,
    )
    headers = dict(observed[0]["headers"])
    assert headers[b"cache-control"] == b"no-store"
    assert UUID(headers[b"x-request-id"].decode()).version == 4


def test_trusted_programmatic_request_accepts_a_calendar_date() -> None:

    assert TriggerRequest(as_of=NOW.date()).as_of == NOW.date()


@pytest.mark.asvs("16.2.5", "16.5.1")
async def test_raw_dependency_exception_cannot_reach_server_logging(
    caplog: pytest.LogCaptureFixture,
) -> None:
    harness = Harness()
    harness.runner.fail = True
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(
            app=harness.app, raise_app_exceptions=True
        ),
        base_url="https://service.test",
        headers={"Authorization": "Bearer " + harness.token()},
    ) as client:
        response = await client.post("/v1/runs", json={"as_of": "2026-10-21"})
    assert response.status_code == 500
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["request_id"] == response.headers["x-request-id"]
    assert "rawnote/secret" not in response.text
    assert "rawnote/secret" not in caplog.text
    assert "rawnote/secret" not in str(harness.sink.events)
    assert any(
        event.name == "api.unhandled_error" for event in harness.sink.events
    )


@pytest.mark.parametrize("sent", ["headers", "partial", "finished"])
async def test_exception_after_response_start_never_sends_second_headers(
    sent: str,
) -> None:
    observed: list[Message] = []
    sink = RecordingSink()
    observer = Observer(sink)

    async def receive() -> Message:
        return {"type": "http.request", "body": b""}

    async def send(message: Message) -> None:
        observed.append(message)

    async def broken(scope: Scope, receive: Receive, send: Send) -> None:
        await send(
            {"type": "http.response.start", "status": 200, "headers": []}
        )
        if sent != "headers":
            await send(
                {
                    "type": "http.response.body",
                    "body": b"partial",
                    "more_body": sent != "finished",
                }
            )
        message = "rawnote/secret"
        raise RuntimeError(message)

    await SecurityMiddleware(broken, ApiSettings(), observer)(
        {"type": "http"}, receive, send
    )
    assert (
        sum(message["type"] == "http.response.start" for message in observed)
        == 1
    )
    assert observed[-1]["type"] == "http.response.body"
    assert not observed[-1].get("more_body", False)
    assert sink.events[-1].name == "api.unhandled_error"
    assert b"rawnote/secret" not in b"".join(
        message.get("body", b"") for message in observed
    )


async def test_lifespan_errors_propagate_to_the_server() -> None:
    async def broken(scope: Scope, receive: Receive, send: Send) -> None:
        message = "startup must fail"
        raise RuntimeError(message)

    async def receive() -> Message:
        return {"type": "lifespan.startup"}

    async def send(message: Message) -> None:
        pytest.fail("startup failures cannot become HTTP responses")

    with pytest.raises(RuntimeError, match="startup must fail"):
        await SecurityMiddleware(broken, ApiSettings(), Observer())(
            {"type": "lifespan"}, receive, send
        )
