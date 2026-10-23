import json

import httpx
import pytest
import structlog

from api_support import Harness
from processing_support import stack
from techcom_followup.adapters.inbound.api.app import create_app
from techcom_followup.infrastructure.log_config import configure_logging


async def test_api_processing_and_project_scoped_reads() -> None:
    identity = Harness()
    async with stack() as workflow:
        app = create_app(
            repository=workflow.repository,
            runner=workflow.processor,
            verifier=identity.verifier,
            clock=identity.clock,
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="https://api.test",
            headers={"Authorization": "Bearer " + identity.token()},
        ) as client:
            first = await client.post("/v1/runs", json={"as_of": "2026-10-21"})
            assert first.status_code == 200
            assert first.json()["enqueued"] == 7
            saved = await client.get("/v1/runs/" + first.json()["run_id"])
            assert saved.status_code == 200
            assert saved.json()["counts"]["decisions"] == 10
            second = await client.post(
                "/v1/runs", json={"as_of": "2026-10-21"}
            )
            assert second.status_code == 200
            assert second.json()["enqueued"] == 0
            assert second.json()["duplicates"] == 7
            assert len(await workflow.repository.list_actions()) == 7
            token = identity.token(
                sub="project-reader",
                projects=["SURF Illinois"],
                scope="actions:read",
            )
            actions = await client.get(
                "/v1/actions", headers={"Authorization": "Bearer " + token}
            )
            assert actions.status_code == 200
            assert actions.json()
            assert {row["target"]["project"] for row in actions.json()} == {
                "SURF Illinois"
            }
            denied = await client.get(
                "/v1/actions",
                params={"project": "Michigan Expansion"},
                headers={"Authorization": "Bearer " + token},
            )
            assert denied.status_code == 200
            assert denied.json() == []
            scoped_trigger = await client.post(
                "/v1/runs",
                json={"as_of": "2026-10-21"},
                headers={"Authorization": "Bearer " + token},
            )
            assert scoped_trigger.status_code == 403
        assert not workflow.store._entries


async def test_api_dry_run_never_creates_outbox_work() -> None:
    identity = Harness()
    async with stack() as workflow:
        app = create_app(
            repository=workflow.repository,
            runner=workflow.processor,
            verifier=identity.verifier,
            clock=identity.clock,
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://api.test"
        ) as client:
            response = await client.post(
                "/v1/runs",
                json={"as_of": "2026-10-21", "dry_run": True},
                headers={"Authorization": "Bearer " + identity.token()},
            )
        assert response.status_code == 200
        assert response.json()["run_id"] is None
        assert response.json()["status"] == "dry_run"
        assert not await workflow.repository.list_actions()
        assert not workflow.store._entries


async def test_api_logging_preserves_security_events_and_correlation(
    capsys: pytest.CaptureFixture[str],
) -> None:
    identity = Harness()
    token = identity.token()
    previous = structlog.get_config()
    try:
        configure_logging()
        async with stack() as workflow:
            app = create_app(
                repository=workflow.repository,
                runner=workflow.processor,
                verifier=identity.verifier,
                clock=identity.clock,
            )
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="https://api.test",
            ) as client:
                accepted = await client.get(
                    "/v1/actions", headers={"Authorization": "Bearer " + token}
                )
                rejected = await client.get("/v1/actions")
        assert accepted.status_code == 200
        assert rejected.status_code == 401
    finally:
        structlog.configure(**previous)
    captured = capsys.readouterr()
    assert not captured.out
    assert token not in captured.err
    events = [json.loads(line) for line in captured.err.splitlines()]
    success = next(
        row for row in events if row["event"] == "security.authn_succeeded"
    )
    failure = next(
        row for row in events if row["event"] == "security.authn_failed"
    )
    assert success["request_id"] == accepted.headers["x-request-id"]
    assert len(success["actor_id"]) == 24
    assert failure["request_id"] == rejected.headers["x-request-id"]
    assert failure["status"] == 401
