import asyncio
from collections.abc import AsyncIterator
from typing import override
from uuid import UUID, uuid4

import httpx
import pytest

from api_support import NOW, FakeRunResult, Harness, seed_run
from support import AS_OF
from techcom_followup.adapters.inbound.api.app import (
    ROUTE_SCOPES,
    create_app,
    create_metrics_app,
)
from techcom_followup.adapters.inbound.api.middleware import (
    MAX_REQUEST_BYTES,
    ApiSettings,
)
from techcom_followup.adapters.inbound.api.observability import Observer
from techcom_followup.adapters.outbound.in_memory_repository import (
    InMemoryRepository,
)
from techcom_followup.application.repository import RunRecord


@pytest.mark.asvs("13.4.5", "14.3.2", "3.4.1", "3.4.4")
async def test_public_surface_and_security_headers() -> None:
    harness = Harness()
    async with harness.client() as client:
        for path in ("/healthz", "/readyz"):
            response = await client.get(path)
            assert response.json() == {"status": "ok"}
            assert response.status_code == 200
        for path in (
            "/metrics",
            "/docs",
            "/redoc",
            "/openapi.json",
            "/unknown",
        ):
            response = await client.get(path)
            assert response.status_code == 404
            assert response.headers["cache-control"] == "no-store"
            assert response.headers["x-content-type-options"] == "nosniff"
            assert response.headers["referrer-policy"] == "no-referrer"
            assert (
                response.headers["content-security-policy"]
                == "default-src 'none'; frame-ancestors 'none'"
            )
            assert response.headers["strict-transport-security"].startswith(
                "max-age=31536000"
            )
            assert "access-control-allow-origin" not in response.headers
        assert (
            await client.request("TRACE", "/v1/actions")
        ).status_code == 405
    assert harness.app.debug is False


@pytest.mark.asvs("8.2.1", "9.1.1")
async def test_all_business_routes_require_authentication_and_scope() -> None:
    harness = Harness()
    protected = {
        (method.upper(), path)
        for path, operations in harness.app.openapi()["paths"].items()
        for method in operations
        if path.startswith("/v1")
    }
    assert protected == set(ROUTE_SCOPES)
    paths = [
        ("POST", "/v1/runs", {"as_of": "2026-10-21"}),
        ("GET", f"/v1/runs/{uuid4()}", None),
        ("GET", "/v1/actions", None),
    ]
    async with harness.client() as client:
        for method, path, body in paths:
            response = await client.request(method, path, json=body)
            assert response.status_code == 401
            assert response.headers["www-authenticate"] == "Bearer"
    async with harness.client(harness.token(scope="")) as client:
        for method, path, body in paths:
            assert (
                await client.request(method, path, json=body)
            ).status_code == 403
    assert harness.runner.calls == []


@pytest.mark.asvs("8.2.1")
async def test_new_router_route_without_a_declared_scope_is_denied() -> None:
    harness = Harness()
    del ROUTE_SCOPES[("GET", "/v1/actions")]
    try:
        async with harness.client(harness.token()) as client:
            assert (await client.get("/v1/actions")).status_code == 403
    finally:
        ROUTE_SCOPES[("GET", "/v1/actions")] = "actions:read"


@pytest.mark.asvs("8.2.2", "10.3.2")
async def test_trigger_requires_explicit_all_project_service() -> None:
    harness = Harness()
    scoped = harness.token(sub="project-reader", projects=["A"])
    async with harness.client(scoped) as client:
        response = await client.post("/v1/runs", json={"as_of": "2026-10-21"})
        assert response.status_code == 403
    assert harness.runner.calls == []
    harness.runner.result = FakeRunResult(uuid4(), "completed", 2, 1)
    async with harness.client(harness.token()) as client:
        response = await client.post(
            "/v1/runs", json={"as_of": "2026-10-21", "dry_run": True}
        )
    assert response.status_code == 200
    assert response.json()["enqueued"] == 2
    assert harness.runner.calls == [(AS_OF, True)]
    assert set(response.json()) == {
        "run_id",
        "status",
        "enqueued",
        "duplicates",
        "stale",
    }


@pytest.mark.asvs("2.4.1")
async def test_trigger_concurrency_and_cancellation_release_lock() -> None:
    harness = Harness()
    harness.runner.block = True
    async with harness.client(harness.token()) as client:
        first = asyncio.create_task(
            client.post("/v1/runs", json={"as_of": "2026-10-21"})
        )
        await harness.runner.entered.wait()
        assert (
            await client.post("/v1/runs", json={"as_of": "2026-10-21"})
        ).status_code == 409
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        harness.runner.release.set()
        assert (
            await client.post("/v1/runs", json={"as_of": "2026-10-21"})
        ).status_code == 200
    assert len(harness.runner.calls) == 2


@pytest.mark.asvs("8.2.2", "8.1.2")
async def test_scoped_run_graph_and_global_report_visibility() -> None:
    harness = Harness()
    safe = await seed_run(harness, ("A",))
    mixed = await seed_run(harness, ("A", "B"))
    async with harness.client(
        harness.token(sub="reader", projects=["A"])
    ) as client:
        response = await client.get(f"/v1/runs/{safe.id}")
        assert response.status_code == 200
        assert response.json()["counts"]["decisions"] == 1
        foreign = await client.get(f"/v1/runs/{mixed.id}")
        missing = await client.get(f"/v1/runs/{uuid4()}")
        assert foreign.status_code == missing.status_code == 404
        assert foreign.json().keys() == missing.json().keys()
    async with harness.client(harness.token()) as client:
        response = await client.get(f"/v1/runs/{mixed.id}")
        assert response.status_code == 200
        assert response.json()["counts"]["decisions"] == 2
        assert "message" not in response.text
        assert "John Smith" not in response.text


@pytest.mark.asvs("8.2.2")
@pytest.mark.parametrize(
    "variant",
    [
        "empty",
        "quiet",
        "unknown_project",
        "unfinished",
        "rejected",
        "wrong_count",
        "failed",
    ],
)
async def test_ambiguous_run_project_provenance_is_hidden(
    variant: str,
) -> None:
    harness = Harness()
    if variant in {"wrong_count", "failed"}:
        run = await seed_run(harness, ("A",), finish=False)
        await harness.repo.finish_run(
            run.id,
            {
                "status": "failed" if variant == "failed" else "completed",
                "decisions": 99,
                "rejected": 0,
            },
            NOW,
        )
    else:
        run = await seed_run(
            harness,
            ()
            if variant == "empty"
            else (None,)
            if variant == "unknown_project"
            else ("A",),
            quiet=variant == "quiet",
            finish=variant != "unfinished",
            rejected=1 if variant == "rejected" else 0,
        )
    async with harness.client(
        harness.token(sub="reader", projects=["A"])
    ) as client:
        assert (await client.get(f"/v1/runs/{run.id}")).status_code == 404
    async with harness.client(harness.token()) as client:
        assert (await client.get(f"/v1/runs/{run.id}")).status_code == 200


@pytest.mark.asvs("8.2.2", "8.1.2")
async def test_action_filters_only_narrow_server_stored_memberships() -> None:
    harness = Harness()
    await seed_run(harness, ("B", "A", None))
    async with harness.client(
        harness.token(sub="reader", projects=["A"])
    ) as client:
        response = await client.get("/v1/actions?limit=1")
        assert response.status_code == 200
        assert len(response.json()) == 1
        row = response.json()[0]
        assert row["target"]["project"] == "A"
        assert row["task_id"] == "TC-1"
        assert "lease_token" not in row
        assert "idempotency_key" not in row
        for query in ("project=B", "task_id=TC-0", "status=sent"):
            assert (await client.get("/v1/actions?" + query)).json() == []
        assert (
            len(
                (
                    await client.get(
                        "/v1/actions?task_id=TC-1&status=pending&project=A"
                    )
                ).json()
            )
            == 1
        )
    async with harness.client(harness.token()) as client:
        assert len((await client.get("/v1/actions")).json()) == 3


@pytest.mark.asvs("2.2.1", "15.3.3", "16.5.1")
@pytest.mark.parametrize(
    "body",
    [
        {"as_of": 20261021},
        {"as_of": "20261021"},
        {"as_of": "invalid-secret-note"},
        {"as_of": "2026-10-21", "status": "done"},
        {"as_of": "2026-10-21", "dry_run": "false"},
    ],
)
async def test_invalid_request_never_echoes_input(
    body: dict[str, object],
) -> None:
    harness = Harness()
    async with harness.client(harness.token()) as client:
        response = await client.post("/v1/runs", json=body)
    assert response.status_code == 422
    assert response.headers["content-type"] == "application/problem+json"
    assert set(response.json()) == {"type", "title", "status", "request_id"}
    assert "secret-note" not in response.text
    assert harness.runner.calls == []


@pytest.mark.asvs("2.4.1", "15.2.2")
async def test_limits_use_actual_bytes_and_reject_duplicate_query_fields() -> (
    None
):
    harness = Harness()

    async def oversized() -> AsyncIterator[bytes]:
        yield b"x" * (MAX_REQUEST_BYTES // 2)
        yield b"x" * (MAX_REQUEST_BYTES // 2 + 1)

    async with harness.client(harness.token()) as client:
        response = await client.post(
            "/v1/runs", headers={"Content-Length": "1"}, content=oversized()
        )
        assert response.status_code == 413
        assert (
            await client.get("/v1/actions?limit=1&limit=2")
        ).status_code == 400
        assert (
            await client.get("/v1/actions?" + "x" * 4097)
        ).status_code == 414
        assert (
            await client.get(
                "/v1/actions?" + "&".join(f"x{i}=1" for i in range(17))
            )
        ).status_code == 400
        assert (await client.get("/v1/actions?project=%ff")).status_code == 400
        assert (
            await client.get("/v1/actions?status=invalid-secret-note")
        ).status_code == 422
        assert (await client.get("/v1/actions?unknown=1")).status_code == 422
    assert harness.runner.calls == []


@pytest.mark.asvs("2.4.1", "6.3.1")
async def test_rate_limit_follows_subject_and_refills() -> None:
    harness = Harness(ApiSettings(rate_capacity=1, rate_refill_s=60))
    async with harness.client(harness.token()) as client:
        assert (await client.get("/v1/actions")).status_code == 200
        client.headers["Authorization"] = "Bearer " + harness.token()
        limited = await client.get("/v1/actions")
        assert limited.status_code == 429
        assert limited.headers["Retry-After"] == "60"
        harness.clock.advance(60)
        client.headers["Authorization"] = "Bearer " + harness.token()
        assert (await client.get("/v1/actions")).status_code == 200
    unauthenticated = Harness(ApiSettings(rate_capacity=1))
    async with unauthenticated.client() as client:
        assert (await client.get("/v1/actions")).status_code == 401
        assert (await client.get("/v1/actions")).status_code == 429


@pytest.mark.asvs("16.5.1", "13.4.2")
async def test_failure_and_readiness_errors_are_generic() -> None:
    harness = Harness()
    harness.runner.fail = True
    async with harness.client(harness.token()) as client:
        failed = await client.post("/v1/runs", json={"as_of": "2026-10-21"})
    assert failed.status_code == 500
    assert "sensitive" not in failed.text
    assert failed.headers["cache-control"] == "no-store"

    class UnavailableRepository(InMemoryRepository):
        @override
        async def get_run(self, run_id: UUID) -> RunRecord | None:
            message = "secret database location"
            raise RuntimeError(message)

    app = create_app(
        repository=UnavailableRepository(),
        runner=harness.runner,
        verifier=harness.verifier,
        clock=harness.clock,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="https://service.test",
    ) as client:
        response = await client.get("/readyz")
    assert response.status_code == 503
    assert "database" not in response.text


@pytest.mark.asvs("16.4.1", "14.3.2")
async def test_request_id_is_canonical_and_untrusted_value_is_not_logged() -> (
    None
):
    harness = Harness(ApiSettings(production=False))
    identifier = uuid4()
    async with harness.client(harness.token()) as client:
        response = await client.get(
            "/v1/actions",
            headers={"X-Request-ID": "{" + str(identifier).upper() + "}"},
        )
        assert response.headers["x-request-id"] == str(identifier)
        assert "strict-transport-security" not in response.headers
        invalid = await client.get(
            "/v1/actions", headers={"X-Request-ID": "forged\r\nsecret"}
        )
        assert (
            str(UUID(invalid.headers["x-request-id"]))
            == invalid.headers["x-request-id"]
        )
    assert "secret" not in str(harness.sink.events)
    assert harness.sink.events


@pytest.mark.asvs("13.4.5")
async def test_metrics_require_a_separate_internal_application() -> None:
    observer = Observer()
    app = create_metrics_app(observer)
    observer.response(200)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://internal"
    ) as client:
        response = await client.get("/metrics")
        assert response.status_code == 200
        assert "followup_api_requests_total" in response.text
        assert (await client.get("/v1/actions")).status_code == 404


@pytest.mark.parametrize("as_of", ["2026-10-19", "2026-11-30", "2099-01-01"])
async def test_saved_runs_need_an_evaluation_date_near_the_clock(
    as_of: str,
) -> None:
    harness = Harness()
    async with harness.client(harness.token()) as client:
        response = await client.post("/v1/runs", json={"as_of": as_of})
        assert response.status_code == 422
        assert harness.runner.calls == []
        # Dry runs persist and send nothing, so any date is allowed.
        dry = await client.post(
            "/v1/runs", json={"as_of": as_of, "dry_run": True}
        )
        assert dry.status_code == 200
