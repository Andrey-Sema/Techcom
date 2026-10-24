"""Upgrades fence existing stale queues and cannot lose uncertainty."""

from dataclasses import replace

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from integration.persistence.test_lifecycle_migration import revision
from persistence_support import NOW, action_write
from support import AS_OF
from techcom_followup.adapters.outbound.pg_repository import PgRepository
from techcom_followup.application.repository import ActionStatus
from techcom_followup.domain.identity import ActionIdentity

pytestmark = pytest.mark.integration


async def test_upgrade_retires_an_already_stale_queue(
    pg_engine: AsyncEngine,
) -> None:
    async with pg_engine.begin() as connection:
        await connection.run_sync(
            lambda sync: revision(sync, "0008", upgrade=False)
        )
    repository = PgRepository(pg_engine)
    write = await action_write(repository)
    await repository.record_decision(write, NOW)
    run = await repository.start_run(AS_OF, "new", NOW)
    newer = replace(write, run_id=run.id, fingerprint="new-state")
    newer = replace(
        newer, identity=ActionIdentity.of(newer.decision, newer.fingerprint)
    )
    await repository.record_decision(newer, NOW)
    # Read the old schema without the new ORM column before upgrading.
    async with pg_engine.connect() as connection:
        statuses = await connection.execute(text("SELECT status FROM actions"))
        assert tuple(statuses.scalars()) == ("pending", "pending")
    async with pg_engine.begin() as connection:
        await connection.run_sync(
            lambda sync: revision(sync, "head", upgrade=True)
        )
    actions = await repository.list_actions()
    assert [item.status for item in actions] == [
        ActionStatus.DEAD,
        ActionStatus.PENDING,
    ]


async def test_downgrade_cannot_discard_an_uncertain_superseded_fence(
    pg_engine: AsyncEngine,
) -> None:
    async with pg_engine.begin() as connection:
        await connection.run_sync(
            lambda sync: revision(sync, "0009", upgrade=False)
        )
    repository = PgRepository(pg_engine)
    await repository.record_decision(await action_write(repository), NOW)
    async with pg_engine.begin() as connection:
        await connection.execute(
            text("UPDATE actions SET superseded=true, status='unknown'")
        )
    with pytest.raises(DBAPIError, match="superseded uncertain actions"):
        async with pg_engine.begin() as connection:
            await connection.run_sync(
                lambda sync: revision(sync, "0008", upgrade=False)
            )


async def test_legacy_writer_cannot_clear_the_superseded_bit(
    pg_engine: AsyncEngine,
) -> None:
    repository = PgRepository(pg_engine)
    await repository.record_decision(await action_write(repository), NOW)
    async with pg_engine.begin() as connection:
        await connection.execute(
            text("UPDATE actions SET superseded=true, status='unknown'")
        )
        await connection.execute(
            text("UPDATE actions SET superseded=false, status='pending'")
        )
    (action,) = await repository.list_actions()
    assert action.status is ActionStatus.DEAD
    assert action.last_error == "source_state_superseded"
