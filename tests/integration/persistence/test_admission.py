"""Shared limits and durable recipient admission under races."""

import asyncio
from dataclasses import replace
from datetime import timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from persistence_support import NOW, action_write
from techcom_followup.adapters.outbound.admission import (
    PgDeliveryQuota,
    PgRequestLimiter,
)
from techcom_followup.adapters.outbound.pg_repository import PgRepository
from techcom_followup.application.repository import ClaimRequest

pytestmark = pytest.mark.integration


async def test_null_arguments_do_not_reserve_or_mutate_limits(
    pg_engine: AsyncEngine,
) -> None:
    async with pg_engine.begin() as connection:
        result = await connection.execute(
            text(
                "SELECT public.admit_api_request(NULL::text,NULL::text,"
                "NULL::integer,NULL::double precision,NULL::integer)"
            )
        )
        assert result.scalar_one() is None
        assert (
            await connection.execute(
                text("SELECT count(*) FROM api_rate_buckets")
            )
        ).scalar_one() == 0


async def test_repeatable_read_admission_fails_closed(
    pg_engine: AsyncEngine,
) -> None:
    async def admit_in_repeatable_read() -> None:
        async with pg_engine.begin() as connection:
            await connection.execute(
                text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            )
            await connection.execute(
                text(
                    "SELECT public.admit_api_request("
                    "'requests', :actor, 3, 60, 10)"
                ),
                {"actor": "a" * 64},
            )

    with pytest.raises(DBAPIError, match="invalid admission configuration"):
        await admit_in_repeatable_read()


async def test_independent_limiters_share_atomic_capacity(
    pg_engine: AsyncEngine,
) -> None:
    limiters = [PgRequestLimiter(pg_engine, capacity=3) for _ in range(2)]
    allowed = await asyncio.gather(
        *(
            limiters[index % 2].allow("requests", "same-service")
            for index in range(12)
        )
    )
    assert sum(allowed) == 3
    assert await limiters[0].allow("requests", "another-service")
    assert await limiters[1].allow("failures", "same-service")


async def test_cardinality_cannot_be_raced_and_no_active_eviction(
    pg_engine: AsyncEngine,
) -> None:
    limiter = PgRequestLimiter(pg_engine, capacity=2, max_subjects=2)
    allowed = await asyncio.gather(
        *(limiter.allow("requests", str(index)) for index in range(10))
    )
    assert sum(allowed) == 2
    accepted = [str(index) for index, ok in enumerate(allowed) if ok]
    assert all(
        await asyncio.gather(
            *(limiter.allow("requests", actor) for actor in accepted)
        )
    )


async def test_quota_counts_operations_once_and_fences_stale_tokens(
    pg_engine: AsyncEngine,
) -> None:
    repository = PgRepository(pg_engine)
    for index in range(12):
        await repository.record_decision(
            await action_write(repository, f"TC-{index}"), NOW
        )
    claimed = await repository.claim_actions(ClaimRequest(NOW, limit=12))
    quota = PgDeliveryQuota(pg_engine, daily_limit=3)
    results = await asyncio.gather(
        *(quota.allow(action, NOW) for action in claimed)
    )
    assert sum(results) == 3
    for action, allowed in zip(claimed, results, strict=True):
        assert await quota.allow(action, NOW) is allowed
        assert not await quota.allow(replace(action, lease_token=None), NOW)
        assert not await quota.allow(action, NOW + timedelta(minutes=1))
    async with pg_engine.connect() as connection:
        assert (
            await connection.execute(
                text("SELECT sum(used) FROM recipient_days")
            )
        ).scalar_one() == 3


async def test_service_cannot_erase_tombstones_or_edit_quota_tables(
    pg_engine: AsyncEngine,
) -> None:
    async with pg_engine.connect() as connection:
        for table in (
            "action_keys",
            "recipient_days",
            "recipient_admissions",
            "api_rate_buckets",
        ):
            for privilege in ("INSERT", "UPDATE", "DELETE", "TRUNCATE"):
                value = await connection.execute(
                    text(
                        "SELECT has_table_privilege("
                        "'followup_app', :table, :privilege)"
                    ),
                    {"table": table, "privilege": privilege},
                )
                assert value.scalar_one() is False
        assert (
            await connection.execute(
                text(
                    "SELECT has_table_privilege('followup_maintenance',"
                    "'action_keys','DELETE')"
                )
            )
        ).scalar_one() is False
