from dataclasses import replace

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from contract.persistence.test_source_versions import next_write, version
from integration.persistence.test_lifecycle_migration import revision
from persistence_support import NOW, action_write
from techcom_followup.adapters.outbound.pg_repository import PgRepository
from techcom_followup.application.repository import DecisionOutcome

pytestmark = pytest.mark.integration


async def test_populated_watermark_cannot_be_silently_downgraded(
    pg_engine: AsyncEngine,
) -> None:
    repository = PgRepository(pg_engine)
    write = replace(await action_write(repository), source_version=version(21))
    await repository.record_decision(write, NOW)
    with pytest.raises(DBAPIError, match="source versions prevent downgrade"):
        async with pg_engine.begin() as connection:
            await connection.run_sync(
                lambda sync: revision(sync, "0009", upgrade=False)
            )
    stale = await next_write(repository, write, version(20), "old-state")
    assert (
        await repository.record_decision(stale, NOW)
    ).outcome is DecisionOutcome.STALE_SUPPRESSED


async def test_legacy_enqueue_cannot_consume_identity_for_a_stale_decision(
    pg_engine: AsyncEngine,
) -> None:
    repository = PgRepository(pg_engine)
    write = replace(await action_write(repository), source_version=version(21))
    await repository.record_decision(write, NOW)
    stale = await next_write(repository, write, version(20), "old-state")
    stored = await repository.record_decision(stale, NOW)
    assert stale.identity is not None
    async with pg_engine.begin() as connection:
        result = await connection.execute(
            text("""
            INSERT INTO actions
                (decision_id,idempotency_key,operation_id,next_attempt_at)
            VALUES (:decision,:key,:operation,:now) RETURNING id
        """),
            {
                "decision": stored.id,
                "key": stale.identity.idempotency_key,
                "operation": stale.identity.operation_id,
                "now": NOW,
            },
        )
        assert result.scalar_one_or_none() is None
        keys = await connection.execute(
            text("SELECT count(*) FROM action_keys")
        )
        assert keys.scalar_one() == 1
