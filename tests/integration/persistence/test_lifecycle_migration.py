"""Upgrade restores identities; rollback cannot erase tombstones."""

from dataclasses import replace

import pytest
from alembic import command
from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from persistence_support import NOW, action_write, migration_config
from support import AS_OF
from techcom_followup.adapters.outbound.pg_repository import PgRepository
from techcom_followup.application.repository import (
    ActionStatus,
    DecisionOutcome,
)
from techcom_followup.domain.models import Flag

pytestmark = pytest.mark.integration


async def test_legacy_terminal_update_gets_a_safe_retention_timestamp(
    pg_engine: AsyncEngine,
) -> None:
    repository = PgRepository(pg_engine)
    await repository.record_decision(await action_write(repository), NOW)
    async with pg_engine.begin() as connection:
        await connection.execute(
            text("UPDATE actions SET status='dead', terminal_at=NULL")
        )
    (action,) = await repository.list_actions()
    assert action.terminal_at is not None


def revision(connection: Connection, target: str, *, upgrade: bool) -> None:
    config = migration_config()
    config.attributes["connection"] = connection
    operation = command.upgrade if upgrade else command.downgrade
    operation(config, target)


async def test_upgrade_recovers_identity_from_surviving_decision_receipt(
    pg_engine: AsyncEngine,
) -> None:
    repository = PgRepository(pg_engine)
    async with pg_engine.begin() as connection:
        await connection.run_sync(
            lambda sync: revision(sync, "0005", upgrade=False)
        )
    write = await action_write(repository)
    await repository.record_decision(write, NOW)
    async with pg_engine.begin() as connection:
        await connection.run_sync(
            lambda sync: revision(sync, "0003", upgrade=False)
        )
        # Model old retention: payload gone, decision still recoverable.
        await connection.execute(text("DELETE FROM actions"))
        await connection.run_sync(
            lambda sync: revision(sync, "head", upgrade=True)
        )
    run = await repository.start_run(AS_OF, "v", NOW)
    replay = await repository.record_decision(
        replace(write, run_id=run.id), NOW
    )
    assert replay.outcome == DecisionOutcome.DUPLICATE_SUPPRESSED
    assert await repository.list_actions() == ()


async def test_downgrade_refuses_to_erase_an_identity_without_payload(
    pg_engine: AsyncEngine,
) -> None:
    repository = PgRepository(pg_engine)
    await repository.record_decision(await action_write(repository), NOW)
    async with pg_engine.begin() as connection:
        await connection.execute(text("DELETE FROM actions"))
    with pytest.raises(
        DBAPIError, match="retained identities prevent downgrade"
    ):
        async with pg_engine.begin() as connection:
            await connection.run_sync(
                lambda sync: revision(sync, "0003", upgrade=False)
            )
    async with pg_engine.connect() as connection:
        assert (
            await connection.execute(text("SELECT count(*) FROM action_keys"))
        ).scalar_one() == 1


async def test_downgrade_refuses_while_an_action_awaits_review(
    pg_engine: AsyncEngine,
) -> None:
    repository = PgRepository(pg_engine)
    write = await action_write(repository)
    held = replace(
        write,
        decision=replace(
            write.decision,
            flags=write.decision.flags | {Flag.HELD_FOR_REVIEW},
        ),
    )
    await repository.record_decision(held, NOW)
    with pytest.raises(DBAPIError, match="held actions prevent downgrade"):
        async with pg_engine.begin() as connection:
            await connection.run_sync(
                lambda sync: revision(sync, "0006", upgrade=False)
            )
    (action,) = await repository.list_actions()
    assert action.status is ActionStatus.HELD
