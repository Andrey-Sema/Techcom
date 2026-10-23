"""One daily LLM budget for every worker (OWASP LLM10)."""

import asyncio
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from techcom_followup.adapters.outbound.admission import PgSpendLedger

pytestmark = pytest.mark.integration

CENT = Decimal("0.01")


async def day_row(engine: AsyncEngine) -> tuple[Decimal, Decimal]:
    async with engine.connect() as connection:
        row = (
            await connection.execute(
                text("SELECT spent_usd, reserved_usd FROM llm_spend_days")
            )
        ).one()
    return row.spent_usd, row.reserved_usd


@pytest.mark.owasp_llm("LLM10")
async def test_concurrent_workers_cannot_overshoot_the_limit(
    pg_engine: AsyncEngine,
) -> None:
    ledgers = [PgSpendLedger(pg_engine, limit_usd=CENT * 3) for _ in range(8)]
    tickets = await asyncio.gather(
        *(ledger.reserve(CENT) for ledger in ledgers)
    )
    admitted = [ticket for ticket in tickets if ticket is not None]
    assert len(admitted) == 3
    assert await day_row(pg_engine) == (Decimal(0), CENT * 3)
    await ledgers[0].settle(admitted[0], Decimal("0.004"))
    assert await day_row(pg_engine) == (Decimal("0.004"), CENT * 2)
    # The freed headroom (0.006) is less than another full bound.
    assert await ledgers[1].reserve(CENT) is None
    assert await ledgers[1].reserve(Decimal("0.006")) is not None


async def test_overrun_is_charged_in_full_and_closes_admission(
    pg_engine: AsyncEngine,
) -> None:
    ledger = PgSpendLedger(pg_engine, limit_usd=CENT * 2)
    ticket = await ledger.reserve(CENT)
    assert ticket is not None
    await ledger.settle(ticket, CENT * 3)
    assert await day_row(pg_engine) == (CENT * 3, Decimal(0))
    assert await ledger.reserve(Decimal(0)) is None
    # Settling twice cannot release or double-charge anything.
    await ledger.settle(ticket, CENT)
    assert await day_row(pg_engine) == (CENT * 3, Decimal(0))


async def test_invalid_bounds_are_rejected_by_the_database(
    pg_engine: AsyncEngine,
) -> None:
    async with pg_engine.connect() as connection:
        with pytest.raises(DBAPIError, match="invalid spend bound"):
            await connection.execute(text("SELECT reserve_llm_spend(-1, 5)"))


async def test_unreachable_ledger_admits_nothing(
    pg_engine: AsyncEngine,
) -> None:
    broken = create_async_engine(
        pg_engine.url.set(port=1), connect_args={"connect_timeout": 1}
    )
    try:
        ledger = PgSpendLedger(broken, limit_usd=Decimal(5))
        assert await ledger.reserve(CENT) is None
        await ledger.settle(
            await PgSpendLedger(pg_engine, limit_usd=Decimal(5)).reserve(CENT)
            or pytest.fail("reservation refused"),
            CENT,
        )
    finally:
        await broken.dispose()
