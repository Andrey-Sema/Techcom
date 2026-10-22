"""Least-privilege database role (decision D3 in docs/15).

The service connects as a login role that is a member of
``followup_app``; migration 0002 grants that role DML only and keeps
the audit trail append-only.
"""

import secrets
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import timedelta

import psycopg
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from persistence_support import NOW, action_write
from techcom_followup.adapters.outbound.pg_repository import PgRepository
from techcom_followup.application.repository import (
    ActionStatus,
    ActionUpdate,
    AuditEvent,
    ClaimRequest,
)

pytestmark = pytest.mark.integration

INSUFFICIENT_PRIVILEGE = "42501"
type LoginFactory = Callable[[str], Awaitable[AsyncEngine]]


@pytest.fixture
async def login_as(pg_engine: AsyncEngine) -> AsyncIterator[LoginFactory]:
    """Create throwaway login roles and engines that connect as them."""
    created: list[tuple[str, AsyncEngine]] = []

    async def login(membership: str) -> AsyncEngine:
        name = f"login_{secrets.token_hex(6)}"
        # token_urlsafe only yields [A-Za-z0-9_-]: safe inside quotes.
        password = secrets.token_urlsafe(24)
        async with pg_engine.begin() as connection:
            await connection.execute(
                text(
                    f"CREATE ROLE {name} LOGIN PASSWORD '{password}'"
                    f"{membership}"
                )
            )
        url = pg_engine.url.set(username=name, password=password)
        engine = create_async_engine(url, poolclass=NullPool)
        created.append((name, engine))
        return engine

    yield login
    for name, engine in created:
        await engine.dispose()
        async with pg_engine.begin() as connection:
            await connection.execute(text(f"DROP ROLE {name}"))


@pytest.mark.asvs("13.2.2")
async def test_service_role_runs_the_whole_repository(
    login_as: LoginFactory,
) -> None:
    repository = PgRepository(await login_as(" IN ROLE followup_app"))
    write = await action_write(repository)
    await repository.record_decision(write, NOW)
    (claimed,) = await repository.claim_actions(ClaimRequest(NOW))
    assert await repository.apply_outcome(
        ActionUpdate(
            claimed.id,
            claimed.lease_token or pytest.fail("claim without a lease"),
            NOW,
            ActionStatus.SENT,
            NOW + timedelta(minutes=1),
            provider_ref="mock-1",
        )
    )
    await repository.append_audit(AuditEvent("test.event", NOW, {"ok": True}))
    await repository.finish_run(write.run_id, {"actions": 1}, NOW)
    assert (await repository.list_actions())[0].status is ActionStatus.SENT
    assert len(await repository.list_audit()) == 2


@pytest.mark.asvs("13.2.2", "16.4.2")
@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE audit_events SET kind = 'forged'",
        "DELETE FROM audit_events",
        "TRUNCATE audit_events",
        "DELETE FROM actions",
        "DELETE FROM decisions",
        "TRUNCATE runs CASCADE",
        "UPDATE alembic_version SET version_num = '0000'",
        "DROP TABLE runs",
        "ALTER TABLE actions DROP COLUMN last_error",
        "CREATE TABLE intruder (id integer)",
        (
            "CREATE FUNCTION intruder() RETURNS integer"
            " LANGUAGE sql AS 'SELECT 1'"
        ),
    ],
)
async def test_service_role_cannot_rewrite_history_or_schema(
    login_as: LoginFactory, statement: str
) -> None:
    engine = await login_as(" IN ROLE followup_app")
    async with engine.connect() as connection:
        with pytest.raises(DBAPIError) as caught:
            await connection.execute(text(statement))
    assert isinstance(caught.value.orig, psycopg.Error)
    assert caught.value.orig.sqlstate == INSUFFICIENT_PRIVILEGE


@pytest.mark.asvs("13.2.2")
async def test_other_roles_cannot_even_connect(
    login_as: LoginFactory,
) -> None:
    engine = await login_as("")
    with pytest.raises(DBAPIError, match="permission denied for database"):
        async with engine.connect():
            pass
