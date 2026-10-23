"""Shared PostgreSQL 18 lifecycle and atomic-write test data."""

import os
import secrets
import socket
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime
from urllib.parse import urlsplit

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool
from testcontainers.community.postgres import PostgresContainer

from support import AS_OF, ROOT, make_task, repo_policy
from techcom_followup.application.repository import DecisionWrite, Repository
from techcom_followup.domain.identity import ActionIdentity, state_fingerprint
from techcom_followup.domain.messages import compose
from techcom_followup.domain.models import TaskId, TaskStatus

NOW = datetime(2026, 10, 21, 9, tzinfo=UTC)
DEFAULT_DOCKER_HOST = "unix:///var/run/docker.sock"


def docker_unreachable() -> str | None:
    """Return why the Docker daemon cannot be reached, or ``None``.

    The Docker SDK leaks its socket when the daemon refuses the
    connection, and ``filterwarnings = error`` then turns the leak into
    an unrelated error at interpreter shutdown. A plain socket closed by
    ``with`` keeps the real reason visible. Schemes other than ``unix``
    and ``tcp`` are left to testcontainers to judge.
    """
    host = os.environ.get("DOCKER_HOST") or DEFAULT_DOCKER_HOST
    target = urlsplit(host)
    try:
        if target.scheme == "unix":
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(5)
                sock.connect(target.path)
        elif target.scheme == "tcp":
            address = (target.hostname or "localhost", target.port or 2375)
            with socket.create_connection(address, timeout=5):
                pass
    except OSError as error:
        return f"{host}: {error.strerror or type(error).__name__}"
    return None


@pytest.fixture(scope="session")
def postgres_url() -> Iterator[str]:
    configured = os.environ.get("FOLLOWUP_TEST_DATABASE_URL")
    if configured:
        yield configured
        return
    reason = docker_unreachable()
    if reason is not None:
        if os.environ.get("CI"):
            pytest.fail(f"Docker required in CI ({reason})", pytrace=False)
        pytest.skip(f"Docker unavailable ({reason}); PostgreSQL tests need it")
    with PostgresContainer(
        "postgres:18", driver="psycopg", password=secrets.token_urlsafe(24)
    ) as postgres:
        yield postgres.get_connection_url()


def migration_config() -> Config:
    return Config(str(ROOT / "alembic.ini"))


@asynccontextmanager
async def migrated_engine(postgres_url: str) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(postgres_url, poolclass=NullPool)
    async with engine.begin() as connection:

        def upgrade(sync_connection: Connection) -> None:
            config = migration_config()
            config.attributes["connection"] = sync_connection
            command.upgrade(config, "head")

        await connection.run_sync(upgrade)
        await connection.execute(
            text(
                "TRUNCATE action_keys, api_rate_buckets, recipient_days, "
                "recipient_admissions, actions, decisions, runs, "
                "llm_cache, audit_events CASCADE"
            )
        )
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def pg_engine(postgres_url: str) -> AsyncIterator[AsyncEngine]:
    async with migrated_engine(postgres_url) as engine:
        yield engine


async def action_write(
    repository: Repository, task_id: str = "TC-1"
) -> DecisionWrite:
    task = make_task(task_id=TaskId(task_id), due_date=AS_OF)
    policy = repo_policy()
    # A blocked task guarantees an action even with a recent update.
    task = replace(task, status=TaskStatus.BLOCKED)
    decision = policy.decide(task, as_of=AS_OF)
    fingerprint = state_fingerprint(task)
    run = await repository.start_run(AS_OF, policy.version, NOW)
    return DecisionWrite(
        run.id,
        decision,
        fingerprint,
        ActionIdentity.of(decision, fingerprint),
        compose(decision, task, AS_OF),
        source_project=task.project,
    )
