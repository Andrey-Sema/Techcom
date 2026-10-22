"""Container image tests (M8): run the built images, hardened.

Opt-in with ``-m image`` after ``make images``; the default run excludes
them because they need the images, not just Docker. Every container
runs read-only, without capabilities and with ``no-new-privileges``,
exactly as compose runs it.
"""

import json
import os
import shutil
import subprocess
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from uuid import uuid4

import httpx
import pytest
from testcontainers.community.postgres import PostgresContainer
from testcontainers.core.network import Network

pytestmark = pytest.mark.image

RUNTIME = os.environ.get("FOLLOWUP_TEST_IMAGE", "techcom-followup:dev")
MOCK = os.environ.get("FOLLOWUP_TEST_MOCK_IMAGE", "techcom-mock:dev")
PYTHON = ("--entrypoint=python",)
HARDENED = (
    "--rm",
    "--read-only",
    "--cap-drop=ALL",
    "--security-opt=no-new-privileges:true",
)
# Runs inside the runtime image: what must and must not be installed.
INVENTORY = """
import importlib.util as u, json, os, shutil
print(json.dumps({
    "source": [
        p for p in ("/src", "/app/.git", "/app/src", "/app/tests")
        if os.path.exists(p)
    ],
    "product": u.find_spec("techcom_followup") is not None,
    "mocks": u.find_spec("techcom_mocks") is not None,
    "pip": u.find_spec("pip") is not None,
    "tools": [t for t in ("uv", "gcc", "pip", "git") if shutil.which(t)],
}))
"""
DELIVERY: dict[str, Any] = {
    "task_id": "TC-1002",
    "target": {"kind": "team", "name": "Warehouse team"},
    "message": {
        "subject": "Delivery date needed",
        "body": "Please provide the delivery date for TC-1002.",
        "template_id": "request_eta.v1",
    },
}
WRITE_PROBE = """
import pathlib
denied = []
for path in ("/opt/venv/probe", "/app/probe", "/app/config/probe"):
    try:
        pathlib.Path(path).write_text("x")
    except OSError:
        denied.append(path)
print(len(denied))
"""


def docker(*args: str) -> str:
    executable = shutil.which("docker")
    assert executable is not None, "docker CLI not found"
    result = subprocess.run(
        [executable, *args],
        capture_output=True,
        text=True,
        check=False,
        timeout=180,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def test_runtime_runs_as_the_unprivileged_service_user() -> None:
    assert docker("run", *HARDENED, "--entrypoint=id", RUNTIME, "-u") == (
        "10001"
    )


@pytest.mark.asvs("13.4.1", "15.2.3")
def test_runtime_ships_only_the_product() -> None:
    found = json.loads(
        docker("run", *HARDENED, *PYTHON, RUNTIME, "-c", INVENTORY)
    )
    assert found == {
        "source": [],
        "product": True,
        "mocks": False,
        "pip": False,
        "tools": [],
    }


@pytest.mark.asvs("15.2.3")
def test_service_user_cannot_modify_its_own_code() -> None:
    # Without --read-only: file ownership alone must already deny it.
    output = docker("run", "--rm", *PYTHON, RUNTIME, "-c", WRITE_PROBE)
    assert output == "3"


def test_runtime_entry_point_is_the_cli() -> None:
    assert "followup" in docker("run", *HARDENED, RUNTIME, "--help")


def test_runtime_migrates_runs_and_dedupes_end_to_end() -> None:
    """Shipped image with PostgreSQL 18 and the mock, all hardened."""
    with (
        Network() as network,
        PostgresContainer("postgres:18", driver="psycopg")
        .with_network(network)
        .with_network_aliases("db") as postgres,
    ):
        url = (
            f"postgresql+psycopg://{postgres.username}:{postgres.password}"
            f"@db:5432/{postgres.dbname}"
        )
        common = (
            *HARDENED,
            f"--network={network.name}",
            f"--env=FOLLOWUP_DATABASE_URL={url}",
            "--env=FOLLOWUP_TASK_API_URL=http://mock-api:8081",
            "--env=FOLLOWUP_DELIVERY_API_URL=http://mock-api:8081",
        )
        docker(
            "run", *common, "--entrypoint=alembic", RUNTIME, "upgrade", "head"
        )
        mock = docker(
            "run",
            "--detach",
            *HARDENED,
            f"--network={network.name}",
            "--network-alias=mock-api",
            MOCK,
        )
        try:
            run = ("run", "--as-of", "2026-10-21", "--dispatch")
            first = json.loads(docker("run", *common, RUNTIME, *run))
            second = json.loads(docker("run", *common, RUNTIME, *run))
        finally:
            docker("rm", "--force", "--volumes", mock)
    assert first["storage"] == "postgresql"
    assert first["run"]["stats"]["enqueued"] == 7
    assert {action["status"] for action in first["actions"]} == {"sent"}
    assert second["run"]["stats"]["enqueued"] == 0
    assert second["run"]["stats"]["duplicates"] == 7


@contextmanager
def mock_container(volume: str) -> Iterator[str]:
    container = docker(
        "run",
        "--detach",
        *HARDENED,
        "--publish=127.0.0.1::8081",
        f"--volume={volume}:/var/lib/mock",
        MOCK,
    )
    try:
        port = docker("port", container, "8081/tcp").rsplit(":", 1)[1]
        url = f"http://127.0.0.1:{port}"
        wait_for(f"{url}/v1/tasks")
        yield url
    finally:
        docker("rm", "--force", container)


@pytest.fixture
def journal_volume() -> Iterator[str]:
    volume = docker("volume", "create")
    try:
        yield volume
    finally:
        docker("volume", "rm", "--force", volume)


def wait_for(url: str) -> httpx.Response:
    deadline = time.monotonic() + 30
    while True:
        try:
            return httpx.get(url, timeout=2)
        except httpx.TransportError:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.2)


def test_mock_serves_the_fixture_hardened(journal_volume: str) -> None:
    with mock_container(journal_volume) as url:
        response = httpx.get(f"{url}/v1/tasks", timeout=5)
    assert response.status_code == 200
    assert len(response.json()) == 10


def test_mock_journal_survives_a_restart(journal_volume: str) -> None:
    operation_id = str(uuid4())
    headers = {"Idempotency-Key": operation_id}
    with mock_container(journal_volume) as url:
        first = httpx.post(
            f"{url}/v1/messages", headers=headers, json=DELIVERY, timeout=5
        )
    with mock_container(journal_volume) as url:
        replay = httpx.post(
            f"{url}/v1/messages", headers=headers, json=DELIVERY, timeout=5
        )
        lookup = httpx.get(f"{url}/v1/messages/{operation_id}", timeout=5)
    assert first.status_code == replay.status_code == 201
    assert replay.headers["Idempotent-Replayed"] == "true"
    assert replay.json() == first.json() == lookup.json()


def test_mock_runs_as_its_own_user() -> None:
    config = json.loads(docker("image", "inspect", MOCK))[0]["Config"]
    assert config["User"] == "10002:10002"
    assert config["Healthcheck"]["Test"][0] == "CMD"
