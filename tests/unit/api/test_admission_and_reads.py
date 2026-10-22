import asyncio
import base64
import dataclasses
from collections.abc import AsyncIterator
from unittest.mock import patch

import httpx
import pytest

from api_support import Harness, seed_run
from techcom_followup.adapters.inbound.api.middleware import ApiSettings
from techcom_followup.application.repository import (
    ActionReadQuery,
    ActionRecord,
)


@pytest.mark.asvs("2.4.1", "6.3.1")
async def test_unauthenticated_and_unscoped_bodies_are_never_consumed() -> (
    None
):
    harness = Harness()
    consumed = False

    async def blocked() -> AsyncIterator[bytes]:
        nonlocal consumed
        consumed = True
        await asyncio.Event().wait()
        yield b"never"

    async with harness.client() as client:
        response = await client.post("/v1/runs", content=blocked())
        assert response.status_code == 401
    async with harness.client(harness.token(scope="")) as client:
        response = await client.post("/v1/runs", content=blocked())
        assert response.status_code == 403
    scoped = harness.token(sub="scoped", projects=["A"], scope="runs:trigger")
    async with harness.client(scoped) as client:
        response = await client.post("/v1/runs", content=blocked())
        assert response.status_code == 403
    assert not consumed
    assert not harness.runner.calls


@pytest.mark.asvs("2.4.1", "15.2.2")
@pytest.mark.parametrize("path", ["/v1/runs", "/healthz"])
async def test_incomplete_body_has_a_total_deadline(path: str) -> None:
    harness = Harness(ApiSettings(body_timeout_s=0.001))

    async def trickle() -> AsyncIterator[bytes]:
        yield b"{"
        await asyncio.Event().wait()
        yield b"}"

    async with harness.client(harness.token()) as client:
        response = await asyncio.wait_for(
            client.request(
                "POST" if path == "/v1/runs" else "GET",
                path,
                content=trickle(),
            ),
            timeout=1,
        )
    assert response.status_code == 408
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["request_id"] == response.headers["x-request-id"]
    assert not harness.runner.calls


@pytest.mark.asvs("6.3.1")
async def test_malformed_json_cannot_bypass_authentication_limits() -> None:
    harness = Harness(ApiSettings(rate_capacity=1))
    async with harness.client() as client:
        responses = [
            await client.post(
                "/v1/runs",
                content="{",
                headers={"Content-Type": "application/json"},
            )
            for _ in range(3)
        ]
    assert [response.status_code for response in responses] == [401, 429, 429]
    assert not harness.runner.calls
    authenticated = Harness(ApiSettings(rate_capacity=1))
    async with authenticated.client(authenticated.token()) as client:
        responses = [
            await client.post(
                "/v1/runs",
                content="{",
                headers={"Content-Type": "application/json"},
            )
            for _ in range(2)
        ]
    assert [response.status_code for response in responses] == [422, 429]


@pytest.mark.asvs("8.2.1")
@pytest.mark.parametrize("path", ["/v1/future", "/v1"])
async def test_new_business_route_without_scope_is_denied_before_body(
    path: str,
) -> None:
    harness = Harness()
    called = False

    @harness.app.post(path)
    async def future() -> dict[str, bool]:
        nonlocal called
        called = True
        return {"private": True}

    async with harness.client(harness.token()) as client:
        assert (await client.post(path, content="{")).status_code == 403
    assert not called


@pytest.mark.asvs("2.4.1", "8.2.2")
async def test_action_read_never_uses_the_unbounded_snapshot_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = Harness()
    await seed_run(harness, ("B", "A"))
    calls: list[ActionReadQuery] = []
    bounded = harness.repo.query_actions

    async def forbid_snapshot() -> None:
        pytest.fail("the HTTP reader cannot materialize the full outbox")

    async def capture(query: ActionReadQuery) -> tuple[ActionRecord, ...]:
        calls.append(query)
        return await bounded(query)

    monkeypatch.setattr(harness.repo, "list_actions", forbid_snapshot)
    monkeypatch.setattr(harness.repo, "query_actions", capture)
    token = harness.token(sub="reader", projects=["A"], scope="actions:read")
    async with harness.client(token) as client:
        response = await client.get("/v1/actions?limit=1&project=A")
    assert response.status_code == 200
    assert len(response.json()) == 1
    assert calls == [
        ActionReadQuery(projects=frozenset({"A"}), project="A", limit=1)
    ]


@pytest.mark.asvs("16.2.5", "16.5.1")
async def test_receive_errors_stay_inside_the_private_exception_boundary(
    caplog: pytest.LogCaptureFixture,
) -> None:
    harness = Harness()

    async def failing_body() -> AsyncIterator[bytes]:
        yield b"first"
        message = "private-body-decoder-secret"
        raise RuntimeError(message)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(
            app=harness.app, raise_app_exceptions=True
        ),
        base_url="https://service.test",
    ) as client:
        response = await client.request(
            "GET", "/healthz", content=failing_body()
        )
    assert response.status_code == 500
    assert "private-body-decoder-secret" not in response.text + caplog.text
    assert "private-body-decoder-secret" not in str(harness.sink.events)


@pytest.mark.asvs("9.1.1", "6.3.1")
async def test_nested_jwt_and_parser_recursion_are_auth_failures() -> None:
    harness = Harness(ApiSettings(rate_capacity=1))
    header = (
        '{"alg":"EdDSA","typ":"at+jwt","kid":'
        + "[" * 2500
        + "0"
        + "]" * 2500
        + "}"
    )
    token = (
        base64.urlsafe_b64encode(header.encode()).rstrip(b"=").decode()
        + ".e30.AA"
    )
    assert len(token) <= 8192
    async with harness.client(token) as client:
        responses = [await client.get("/v1/actions") for _ in range(2)]
    assert [response.status_code for response in responses] == [401, 429]
    parser = Harness(ApiSettings(rate_capacity=1))
    token = parser.token()
    with patch(
        "jwt.api_jws.json.loads", side_effect=RecursionError("private")
    ):
        async with parser.client(token) as client:
            responses = [await client.get("/v1/actions") for _ in range(2)]
    assert [response.status_code for response in responses] == [401, 429]
    assert all(
        event.name != "api.unhandled_error" for event in parser.sink.events
    )


@pytest.mark.parametrize("timeout", [0, 31, float("nan"), float("inf")])
def test_invalid_body_deadline_is_rejected(timeout: float) -> None:
    with pytest.raises(ValueError, match="positive"):
        ApiSettings(body_timeout_s=timeout)


@pytest.mark.parametrize(
    ("root", "path", "expected"),
    [
        ("/edge", "/edge/v1/actions", 200),
        ("/edge", "/v1/actions", 200),
        ("/edge", "/edgeX/v1/actions", 404),
        ("/edge", "/edge", 404),
    ],
)
async def test_preflight_uses_the_same_mounted_path_boundary(
    root: str, path: str, expected: int
) -> None:
    harness = Harness()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=harness.app, root_path=root),
        base_url="https://service.test",
        headers={"Authorization": "Bearer " + harness.token()},
    ) as client:
        response = await client.get(path)
    assert response.status_code == expected
    if expected == 200:
        assert (
            sum(
                event.name == "security.authn_succeeded"
                for event in harness.sink.events
            )
            == 1
        )


async def test_repository_cannot_broaden_the_api_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = Harness()
    await seed_run(harness, ("A", "B"))
    rows = await harness.repo.list_actions()
    missing_target = dataclasses.replace(
        rows[0], decision=dataclasses.replace(rows[0].decision, target=None)
    )

    async def broken_query(query: ActionReadQuery) -> tuple[ActionRecord, ...]:
        return (*rows, missing_target)

    monkeypatch.setattr(harness.repo, "query_actions", broken_query)
    async with harness.client(
        harness.token(sub="reader", projects=["A"])
    ) as client:
        response = await client.get("/v1/actions")
    assert response.status_code == 200
    assert [row["target"]["project"] for row in response.json()] == ["A"]


async def test_nul_project_filter_is_rejected_before_database_binding() -> (
    None
):
    harness = Harness()
    async with harness.client(harness.token()) as client:
        response = await client.get("/v1/actions", params={"project": "A\0"})
    assert response.status_code == 422
    assert "UTF-8" not in response.text
