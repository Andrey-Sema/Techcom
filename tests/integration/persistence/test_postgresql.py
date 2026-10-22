import asyncio
from contextlib import suppress

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from pydantic import TypeAdapter
from sqlalchemy import event, inspect, select, text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from persistence_support import NOW, action_write, migration_config
from techcom_followup.adapters.outbound.orm import actions, decisions, metadata
from techcom_followup.adapters.outbound.pg_repository import PgRepository
from techcom_followup.application.repository import ClaimRequest, DecisionWrite

pytestmark = pytest.mark.integration


async def test_postgres_generates_uuidv7_and_schema_matches(
    pg_engine: AsyncEngine,
) -> None:
    repository = PgRepository(pg_engine)
    write = await action_write(repository)
    run = await repository.get_run(write.run_id)
    assert run is not None
    assert run.id.version == 7
    stored = await repository.record_decision(write, NOW)
    assert stored.id.version == 7
    assert (await repository.list_actions())[0].id.version == 7
    async with pg_engine.connect() as connection:

        def differences(sync_connection: Connection) -> list[object]:
            context = MigrationContext.configure(sync_connection)
            return list(compare_metadata(context, metadata))

        assert await connection.run_sync(differences) == []


async def test_migration_downgrade_and_upgrade_cleanly(
    pg_engine: AsyncEngine,
) -> None:
    async with pg_engine.begin() as connection:

        def cycle(sync_connection: Connection) -> None:
            config = migration_config()
            config.attributes["connection"] = sync_connection
            command.downgrade(config, "base")
            assert inspect(sync_connection).get_table_names() == [
                "alembic_version"
            ]
            command.upgrade(config, "head")

        await connection.run_sync(cycle)
    assert await PgRepository(pg_engine).list_actions() == ()


async def test_skip_locked_does_not_wait_for_locked_action(
    pg_engine: AsyncEngine,
) -> None:
    repository = PgRepository(pg_engine)
    for identifier in ("TC-1", "TC-2"):
        write = await action_write(repository, identifier)
        await repository.record_decision(write, NOW)
    first = (await repository.list_actions())[0]
    async with pg_engine.begin() as blocker:
        await blocker.execute(
            select(actions).where(actions.c.id == first.id).with_for_update()
        )
        claimed = await asyncio.wait_for(
            repository.claim_actions(ClaimRequest(NOW)), timeout=2
        )
        assert len(claimed) == 1
        assert claimed[0].id != first.id
    remaining = await repository.claim_actions(ClaimRequest(NOW))
    assert len(remaining) == 1
    assert remaining[0].id == first.id


async def test_cancelled_insert_rolls_back_without_orphan_decision(
    pg_engine: AsyncEngine,
) -> None:
    repository = PgRepository(pg_engine)
    write = await action_write(repository)
    started = asyncio.Event()

    def inserting(
        connection: Connection,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        many: bool,
    ) -> None:
        if statement.startswith("INSERT INTO decisions"):
            started.set()

    async with pg_engine.connect() as blocker:
        transaction = await blocker.begin()
        await blocker.execute(
            decisions.insert().values(
                run_id=write.run_id,
                task_id=write.decision.task_id,
                data=TypeAdapter(DecisionWrite).dump_python(
                    write, mode="json"
                ),
                state_fingerprint=write.fingerprint,
                outcome="none",
            )
        )
        event.listen(pg_engine.sync_engine, "before_cursor_execute", inserting)
        task = asyncio.create_task(repository.record_decision(write, NOW))
        try:
            await asyncio.wait_for(started.wait(), timeout=2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            event.remove(
                pg_engine.sync_engine, "before_cursor_execute", inserting
            )
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            await transaction.rollback()
    assert await repository.list_decisions(write.run_id) == ()
    assert await repository.list_actions() == ()
    assert await repository.list_audit() == ()
    await repository.record_decision(write, NOW)
    assert len(await repository.list_actions()) == 1


async def test_cancelled_claim_rolls_back_lease_and_attempt(
    pg_engine: AsyncEngine,
) -> None:
    repository = PgRepository(pg_engine)
    write = await action_write(repository)
    await repository.record_decision(write, NOW)
    started = asyncio.Event()

    def claiming(
        connection: Connection,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        many: bool,
    ) -> None:
        if statement.startswith("UPDATE actions SET status=") and (
            "attempts=(actions.attempts +" in statement
        ):
            started.set()

    event.listen(pg_engine.sync_engine, "before_cursor_execute", claiming)
    task = asyncio.create_task(repository.claim_actions(ClaimRequest(NOW)))
    try:
        await asyncio.wait_for(started.wait(), timeout=2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        event.remove(pg_engine.sync_engine, "before_cursor_execute", claiming)
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
    current = (await repository.list_actions())[0]
    assert current.attempts == 0
    assert current.lease_token is None
    assert len(await repository.claim_actions(ClaimRequest(NOW))) == 1


@pytest.mark.asvs("2.3.3")
async def test_late_failure_rolls_back_action_decision_and_audit(
    pg_engine: AsyncEngine,
) -> None:
    repository = PgRepository(pg_engine)
    write = await action_write(repository)
    async with pg_engine.begin() as connection:
        await connection.execute(
            text("""
            CREATE FUNCTION reject_test_audit() RETURNS trigger
            LANGUAGE plpgsql AS $$
            BEGIN RAISE EXCEPTION 'injected audit failure'; END; $$
        """)
        )
        await connection.execute(
            text("""
            CREATE TRIGGER reject_test_audit BEFORE INSERT ON audit_events
            FOR EACH ROW EXECUTE FUNCTION reject_test_audit()
        """)
        )
    try:
        with pytest.raises(DBAPIError, match="injected audit failure"):
            await repository.record_decision(write, NOW)
    finally:
        async with pg_engine.begin() as connection:
            await connection.execute(
                text("DROP TRIGGER reject_test_audit ON audit_events")
            )
            await connection.execute(text("DROP FUNCTION reject_test_audit()"))
    assert await repository.list_decisions(write.run_id) == ()
    assert await repository.list_actions() == ()
    assert await repository.list_audit() == ()
