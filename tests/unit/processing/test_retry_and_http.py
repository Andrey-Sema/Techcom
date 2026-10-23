import asyncio
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import timedelta
from typing import override
from unittest.mock import patch
from uuid import uuid4

import httpx
import pytest
import stamina
from hypothesis import given
from hypothesis import strategies as st

from persistence_support import NOW, action_write
from techcom_followup.adapters.outbound.clock import FixedClock, SystemClock
from techcom_followup.adapters.outbound.delivery_http import (
    HttpDeliveryGateway,
)
from techcom_followup.adapters.outbound.in_memory_repository import (
    InMemoryRepository,
)
from techcom_followup.adapters.outbound.task_source_http import HttpTaskSource
from techcom_followup.application.processing_ports import DeliveryKind
from techcom_followup.application.retry import RetryBudget, RetryPolicy
from techcom_followup.infrastructure.http_client import (
    MAX_RESPONSE_BYTES,
    HttpEndpoint,
    UpstreamError,
    retry_after,
)


def endpoint(client: httpx.AsyncClient) -> HttpEndpoint:
    return HttpEndpoint(
        client, "https://mock-api", allowed_hosts=frozenset({"mock-api"})
    )


@given(st.integers(0, 10_000), st.integers(0, 10_000))
def test_retry_budget_never_overdrafts(requests: int, calls: int) -> None:
    budget = RetryBudget(requests)
    taken = sum(budget.take() for _ in range(calls))
    assert taken == min(calls, requests // 5)
    assert budget.remaining == requests // 5 - taken


@given(st.integers(1, 10_000), st.floats(0, 1), st.floats(-10_000, 10_000))
def test_retry_delays_are_finite_and_capped(
    attempt: int, jitter: float, server: float
) -> None:
    delay = RetryPolicy().delay(attempt, jitter=jitter, retry_after=server)
    assert 0 <= delay <= 300
    assert delay >= min(300, max(0, server))


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_attempts": 0},
        {"base_seconds": 0},
        {"cap_seconds": 0},
        {"base_seconds": float("inf")},
        {"cap_seconds": float("nan")},
        {"base_seconds": 10, "cap_seconds": 1},
    ],
)
def test_retry_policy_rejects_invalid_bounds(kwargs: dict[str, float]) -> None:
    with pytest.raises(ValueError, match="bounds"):
        RetryPolicy(
            max_attempts=int(kwargs.get("max_attempts", 6)),
            base_seconds=kwargs.get("base_seconds", 2),
            cap_seconds=kwargs.get("cap_seconds", 300),
        )


def test_retry_invalid_inputs_and_nonfinite_server_delay() -> None:
    with pytest.raises(ValueError, match="allocation"):
        RetryBudget(-1)
    for attempt, jitter in [(0, 0.5), (1, -0.1), (1, 1.1)]:
        with pytest.raises(ValueError, match="attempt"):
            RetryPolicy().delay(attempt, jitter=jitter)
    assert RetryPolicy().delay(1, jitter=1, retry_after=float("inf")) == 2


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, 0),
        ("3", 3),
        ("-2", 0),
        ("1000", 300),
        ("nan", 0),
        ("inf", 0),
        ("garbage", 0),
        ("Wed, 21 Oct 2026 09:00:05 GMT", 5),
        ("Wed, 21 Oct 2026 09:00:05", 0),
    ],
)
def test_retry_after_seconds_and_http_dates(
    value: str | None, expected: float
) -> None:
    assert retry_after(value, NOW) == expected


@pytest.mark.asvs("13.2.4", "13.2.5")
@pytest.mark.parametrize(
    "url",
    [
        "http://mock-api",
        "https://attacker.invalid",
        "https://a@mock-api",
        "https://mock-api/path",
        "https://mock-api?q=x",
        "https://mock-api/#x",
        "file:///etc/passwd",
        "http://attacker.invalid",
    ],
)
async def test_unapproved_origins_fail_closed(url: str) -> None:
    async with httpx.AsyncClient() as client:
        with pytest.raises(ValueError, match="origin"):
            HttpEndpoint(client, url, allowed_hosts=frozenset({"mock-api"}))


async def test_local_http_is_explicit_and_redirects_are_disabled() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            302, headers={"Location": "https://attacker.invalid"}
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=True
    ) as client:
        configured = HttpEndpoint(
            client,
            "http://mock-api/",
            allowed_hosts=frozenset({"mock-api"}),
            allow_local_http=True,
        )
        assert (
            await configured.request("GET", "/v1/tasks")
        ).status_code == 302
        assert len(calls) == 1
        assert calls[0].extensions["timeout"] == {
            "connect": 2,
            "read": 5,
            "write": 5,
            "pool": 5,
        }


@pytest.mark.asvs("2.4.1")
async def test_response_size_is_checked_before_append() -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200, content=b"x" * (MAX_RESPONSE_BYTES + 1)
            )
        )
    ) as client:
        with pytest.raises(UpstreamError, match="response_size"):
            await endpoint(client).request("GET", "/v1/tasks")


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, content=b"not json"),
        httpx.Response(200, content=b"\xff"),
        httpx.Response(200, json={}),
        httpx.Response(200, json=[None] * 10001),
        httpx.Response(400),
        httpx.Response(302),
    ],
)
async def test_source_rejects_invalid_envelopes_without_retry(
    response: httpx.Response,
) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return response

    with stamina.set_testing(True, attempts=4, cap=True):
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            source = HttpTaskSource(
                endpoint(client), FixedClock(NOW), budget=RetryBudget(20)
            )
            with pytest.raises(UpstreamError):
                await source.fetch_tasks()
    assert len(calls) == 1


@pytest.mark.parametrize(
    "failure", [httpx.ConnectError, httpx.ReadTimeout, httpx.ReadError]
)
async def test_source_transport_retries_are_budgeted(
    failure: type[httpx.RequestError],
) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        msg = "secret upstream detail"
        raise failure(msg, request=request)

    with stamina.set_testing(True, attempts=4, cap=True):
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            budget = RetryBudget(5)
            source = HttpTaskSource(
                endpoint(client), FixedClock(NOW), budget=budget
            )
            with pytest.raises(UpstreamError, match="source_transient"):
                await source.fetch_tasks()
    assert len(calls) == 2
    assert budget.remaining == 0


async def test_source_total_deadline_cancels_inflight_attempt() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(60)
        return httpx.Response(200, json=[])

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        source = HttpTaskSource(
            endpoint(client), FixedClock(NOW), budget=RetryBudget(20)
        )
        original = asyncio.timeout
        with (
            patch(
                "techcom_followup.adapters.outbound.task_source_http.asyncio.timeout",
                side_effect=lambda _: original(0.001),
            ),
            pytest.raises(TimeoutError),
        ):
            await source.fetch_tasks()


@pytest.mark.parametrize(
    ("status", "kind"),
    [
        (400, DeliveryKind.PERMANENT),
        (401, DeliveryKind.PERMANENT),
        (403, DeliveryKind.PERMANENT),
        (404, DeliveryKind.PERMANENT),
        (422, DeliveryKind.PERMANENT),
        (302, DeliveryKind.PERMANENT),
        (409, DeliveryKind.RETRYABLE),
        (429, DeliveryKind.RETRYABLE),
        # The write may have committed behind a failed gateway.
        (500, DeliveryKind.UNKNOWN),
        (502, DeliveryKind.UNKNOWN),
        (503, DeliveryKind.RETRYABLE),
        (504, DeliveryKind.UNKNOWN),
    ],
)
async def test_delivery_status_classifier_is_single_attempt(
    status: int, kind: DeliveryKind
) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(status, headers={"Retry-After": "3"})

    repository = InMemoryRepository()
    await repository.record_decision(await action_write(repository), NOW)
    action = (await repository.list_actions())[0]
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        result = await HttpDeliveryGateway(
            endpoint(client), FixedClock(NOW)
        ).send(action)
    assert result.kind is kind
    assert len(calls) == 1
    assert calls[0].headers["Idempotency-Key"] == str(
        action.identity.operation_id
    )
    assert result.error == f"http_{status}"
    if kind is DeliveryKind.RETRYABLE:
        assert result.retry_after == 3


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (httpx.ConnectError, DeliveryKind.RETRYABLE),
        (httpx.ConnectTimeout, DeliveryKind.RETRYABLE),
        (httpx.ReadTimeout, DeliveryKind.UNKNOWN),
        (httpx.ReadError, DeliveryKind.UNKNOWN),
        (httpx.WriteError, DeliveryKind.UNKNOWN),
    ],
)
async def test_delivery_transport_classifier_is_conservative(
    failure: type[httpx.RequestError], expected: DeliveryKind
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        msg = "private upstream details"
        raise failure(msg, request=request)

    repository = InMemoryRepository()
    await repository.record_decision(await action_write(repository), NOW)
    action = (await repository.list_actions())[0]
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        gateway = HttpDeliveryGateway(endpoint(client), FixedClock(NOW))
        assert (await gateway.send(action)).kind is expected
        assert (
            await gateway.lookup(action.identity.operation_id)
        ).kind is DeliveryKind.UNKNOWN


@pytest.mark.parametrize(
    "payload",
    [
        b"bad json",
        b"{}",
        b'{"operation_id":"bad","provider_ref":"ref","status":"sent"}',
    ],
)
async def test_malformed_acknowledgement_is_unknown(payload: bytes) -> None:
    repository = InMemoryRepository()
    await repository.record_decision(await action_write(repository), NOW)
    action = (await repository.list_actions())[0]
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(201, content=payload)
        )
    ) as client:
        assert (
            await HttpDeliveryGateway(endpoint(client), FixedClock(NOW)).send(
                action
            )
        ).kind is DeliveryKind.UNKNOWN


async def test_valid_receipt_for_another_operation_is_unknown() -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                json={
                    "operation_id": str(uuid4()),
                    "provider_ref": "ref",
                    "status": "sent",
                },
            )
        )
    ) as client:
        result = await HttpDeliveryGateway(
            endpoint(client), FixedClock(NOW)
        ).lookup(uuid4())
        assert result.kind is DeliveryKind.UNKNOWN
        assert result.error == "receipt_identity"


def test_clocks_validate_and_convert_timezone() -> None:
    assert SystemClock().now().utcoffset() == timedelta(0)
    assert SystemClock().today() == SystemClock().now().date()
    assert FixedClock(NOW).today() == NOW.date()
    with pytest.raises(ValueError, match="timezone-aware"):
        FixedClock(NOW.replace(tzinfo=None)).now()


async def test_delivery_rejects_a_missing_target_before_io() -> None:
    repository = InMemoryRepository()
    await repository.record_decision(await action_write(repository), NOW)
    action = (await repository.list_actions())[0]
    async with httpx.AsyncClient() as client:
        invalid = replace(
            action, decision=replace(action.decision, target=None)
        )
        with pytest.raises(ValueError, match="target"):
            await HttpDeliveryGateway(endpoint(client), FixedClock(NOW)).send(
                invalid
            )


async def test_oversized_delivery_responses_are_unknown() -> None:
    repository = InMemoryRepository()
    await repository.record_decision(await action_write(repository), NOW)
    action = (await repository.list_actions())[0]
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200, content=b"x" * (MAX_RESPONSE_BYTES + 1)
            )
        )
    ) as client:
        gateway = HttpDeliveryGateway(endpoint(client), FixedClock(NOW))
        assert (await gateway.send(action)).kind is DeliveryKind.UNKNOWN
        assert (
            await gateway.lookup(action.identity.operation_id)
        ).kind is DeliveryKind.UNKNOWN


async def test_stamina_honors_retry_after_and_backoff_without_real_sleep() -> (
    None
):
    waits: list[float] = []
    calls = 0

    async def sleep(delay: float) -> None:
        waits.append(delay)

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls < 3:
            return httpx.Response(429, headers={"Retry-After": "2"})
        return httpx.Response(200, json=[])

    with (
        stamina.set_testing(False),
        patch("stamina._core._smart_sleep", side_effect=sleep),
    ):
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            source = HttpTaskSource(
                endpoint(client),
                FixedClock(NOW),
                budget=RetryBudget(20),
                jitter=lambda: 1,
            )
            assert await source.fetch_tasks() == ()
    assert calls == 3
    assert waits == [2, 2]


@pytest.mark.parametrize("operation", ["send", "lookup", "source"])
async def test_slow_stream_cannot_extend_total_request_deadline(
    operation: str,
) -> None:

    class SlowStream(httpx.AsyncByteStream):
        @override
        async def __aiter__(self) -> AsyncIterator[bytes]:
            yield b"{"
            await asyncio.Event().wait()
            yield b"}"

    repository = InMemoryRepository()
    await repository.record_decision(await action_write(repository), NOW)
    action = (await repository.list_actions())[0]
    original = asyncio.timeout
    with patch(
        "techcom_followup.infrastructure.http_client.asyncio.timeout",
        side_effect=lambda value: original(0.001 if value == 10 else value),
    ):
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, stream=SlowStream())
            )
        ) as client:
            gateway = HttpDeliveryGateway(endpoint(client), FixedClock(NOW))
            if operation == "send":
                result = await gateway.send(action)
                assert result.kind is DeliveryKind.UNKNOWN
            elif operation == "lookup":
                result = await gateway.lookup(action.identity.operation_id)
                assert result.kind is DeliveryKind.UNKNOWN
            else:
                source = HttpTaskSource(
                    endpoint(client), FixedClock(NOW), budget=RetryBudget(0)
                )
                with pytest.raises(UpstreamError, match="source_transient"):
                    await source.fetch_tasks()
