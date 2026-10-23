"""PostgreSQL admission shared by all processes using the database."""

from datetime import datetime
from decimal import Decimal
from hashlib import sha256
from math import ceil
from uuid import UUID

import sqlalchemy as sa
import structlog
from sqlalchemy.ext.asyncio import AsyncEngine

from techcom_followup.application.repository import ActionRecord, require_aware

MAX_RECIPIENT_DAILY_LIMIT = 10_000
_log = structlog.get_logger(__name__)


class PgRequestLimiter:
    """Bounded database-clock windows; a down store fails closed."""

    def __init__(
        self,
        engine: AsyncEngine,
        *,
        capacity: int = 60,
        period_s: float = 60,
        max_subjects: int = 1024,
    ) -> None:
        """Share one validated window policy across instances."""
        self._engine = engine
        self._capacity = capacity
        self._period = period_s
        self._subjects = max_subjects

    @property
    def retry_after(self) -> str:
        """Use the full window as a conservative retry interval."""
        return str(max(1, ceil(self._period)))

    async def allow(self, namespace: str, actor_id: str) -> bool:
        """Reserve one request; never persist tokens or raw IPs."""
        async with self._engine.begin() as connection:
            result = await connection.execute(
                sa.text(
                    "SELECT public.admit_api_request("
                    ":ns,:actor,:cap,:period,:subjects)"
                ),
                {
                    "ns": namespace,
                    "actor": sha256(actor_id.encode()).hexdigest(),
                    "cap": self._capacity,
                    "period": self._period,
                    "subjects": self._subjects,
                },
            )
            return bool(result.scalar_one())


class PgDeliveryQuota:
    """Reserve one logical delivery per recipient/UTC day before I/O."""

    def __init__(self, engine: AsyncEngine, *, daily_limit: int = 20) -> None:
        """Configure a finite cap; the database owns day and slots."""
        if (
            type(daily_limit) is not int
            or not 1 <= daily_limit <= MAX_RECIPIENT_DAILY_LIMIT
        ):
            message = "daily recipient limit must be in 1..10000"
            raise ValueError(message)
        self._engine, self._limit = engine, daily_limit

    async def allow(self, action: ActionRecord, now: datetime) -> bool:
        """Fence stale claims; retrying a reserved operation is free."""
        require_aware(now)
        if action.lease_token is None:
            return False
        async with self._engine.begin() as connection:
            result = await connection.execute(
                sa.text(
                    "SELECT public.admit_delivery(:action,:token,:now,:cap)"
                ),
                {
                    "action": action.id,
                    "token": action.lease_token,
                    "now": now,
                    "cap": self._limit,
                },
            )
            return bool(result.scalar_one())


class PgSpendLedger:
    """Share one daily LLM budget across workers and restarts.

    The database owns the day and the arithmetic; this process only
    passes its configured limit and the bounds it already computed.
    """

    def __init__(self, engine: AsyncEngine, *, limit_usd: Decimal) -> None:
        """Bind the configured limit to the shared ledger."""
        self._engine, self._limit = engine, limit_usd

    async def reserve(self, amount_usd: Decimal) -> UUID | None:
        """Reserve a worst-case attempt; ``None`` when the day is spent.

        A ledger that cannot be reached admits nothing (fail closed).
        """
        try:
            async with self._engine.begin() as connection:
                result = await connection.execute(
                    sa.text("SELECT public.reserve_llm_spend(:amount, :cap)"),
                    {"amount": amount_usd, "cap": self._limit},
                )
                ticket: UUID | None = result.scalar_one()
        except (sa.exc.SQLAlchemyError, OSError) as error:
            _log.error(
                "llm.spend_ledger_failed", error_type=type(error).__name__
            )
            return None
        return ticket

    async def settle(self, ticket: UUID, cost_usd: Decimal) -> None:
        """Replace the reservation with the cost actually billed.

        A failed settlement leaves the full bound charged: conservative,
        and never a reason to fail the call that already happened.
        """
        try:
            async with self._engine.begin() as connection:
                await connection.execute(
                    sa.text("SELECT public.settle_llm_spend(:ticket, :cost)"),
                    {"ticket": ticket, "cost": cost_usd},
                )
        except (sa.exc.SQLAlchemyError, OSError) as error:
            _log.error(
                "llm.spend_ledger_failed", error_type=type(error).__name__
            )
