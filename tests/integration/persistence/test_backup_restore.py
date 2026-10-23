"""Backup and restore round trip with the ``make`` commands (docs/18).

A custom-format ``pg_dump`` of a migrated database with data is restored
into a fresh database of the same cluster; rows, the migration version
and the least-privilege grants must survive.
"""

import asyncio
import secrets

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool
from testcontainers.community.postgres import PostgresContainer

from persistence_support import NOW, action_write, migrated_engine
from techcom_followup.adapters.outbound.pg_repository import PgRepository

pytestmark = pytest.mark.integration
# Inside the throwaway database container, not on the host.
DUMP = "/var/lib/postgresql/followup.dump"

COUNTS = """
SELECT (SELECT count(*) FROM runs), (SELECT count(*) FROM decisions),
       (SELECT count(*) FROM actions), (SELECT count(*) FROM audit_events),
       (SELECT version_num FROM alembic_version),
       has_table_privilege('followup_app', 'audit_events', 'UPDATE'),
       has_table_privilege('followup_app', 'audit_events', 'INSERT'),
       (SELECT count(*) FROM action_keys),
       has_table_privilege('followup_app', 'action_keys', 'DELETE'),
       has_function_privilege('followup_app',
           'public.admit_delivery(uuid,uuid,timestamptz,integer)', 'EXECUTE')
"""


async def seed_and_count(url: str) -> tuple[object, ...]:
    async with migrated_engine(url) as engine:
        repository = PgRepository(engine)
        await repository.record_decision(await action_write(repository), NOW)
        async with engine.connect() as connection:
            return tuple((await connection.execute(text(COUNTS))).one())


async def count(url: str) -> tuple[object, ...]:
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            return tuple((await connection.execute(text(COUNTS))).one())
    finally:
        await engine.dispose()


def run(container: PostgresContainer, *command: str) -> None:
    result = container.exec(list(command))
    assert result.exit_code == 0, result.output


def test_custom_format_dump_restores_rows_version_and_grants() -> None:
    with PostgresContainer(
        "postgres:18", driver="psycopg", password=secrets.token_urlsafe(24)
    ) as postgres:
        url = postgres.get_connection_url()
        before = asyncio.run(seed_and_count(url))
        user, database = postgres.username, postgres.dbname
        run(
            postgres,
            "pg_dump", "-U", user, "--format=custom",
            f"--file={DUMP}", database,
        )  # fmt: skip
        run(postgres, "createdb", "-U", user, "restored")
        run(
            postgres,
            "pg_restore", "-U", user, "--dbname=restored",
            "--exit-on-error", "--single-transaction", DUMP,
        )  # fmt: skip
        after = asyncio.run(count(url.rsplit("/", 1)[0] + "/restored"))
    assert before == after
    # One run, one decision, one action, one audit row; head revision;
    # the service role still cannot rewrite the audit trail.
    assert before[:4] == (1, 1, 1, 1)
    assert before[5:7] == (False, True)
    assert before[7] == 1  # Duplicate identity survives full backup/restore.
    assert before[8:] == (False, True)
