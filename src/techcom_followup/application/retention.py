"""Data retention (docs/15 s. 8): delete what policy no longer needs.

Retention never touches work in progress: pending, held, in-flight and
unknown actions stay, and a decision stays while an action refers to
it. Identity tombstones, slots and source watermarks outlive payload
expiry. Pruning never authorizes another delivery of the same logical
action or erases the source ordering guarantee.
"""

from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from techcom_followup.application.repository import (
    PruneCounts,
    RetentionCutoffs,
    require_aware,
)


class RetentionPolicy(BaseModel):
    """Retention per data class from ``config/retention.toml``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}\.\d+$")
    # Internal data (decisions and runs): 30-90 days.
    decision_days: int = Field(ge=30, le=90)
    # Confidential-personal data (actions, audit trail): one year.
    action_days: int = Field(ge=30, le=366)
    audit_days: int = Field(ge=30, le=366)
    # Rows per table and transaction; keeps locks and WAL bursts short.
    batch_size: int = Field(ge=1, le=10_000)
    max_batches: int = Field(ge=1, le=10_000)

    def cutoffs(self, now: datetime) -> RetentionCutoffs:
        """Absolute instants before which rows may be deleted."""
        require_aware(now)
        return RetentionCutoffs(
            cache=now,
            decisions=now - timedelta(days=self.decision_days),
            actions=now - timedelta(days=self.action_days),
            audit=now - timedelta(days=self.audit_days),
        )


class PruneStore(Protocol):
    """The repository operation retention needs."""

    async def prune(
        self, cutoffs: RetentionCutoffs, limit: int
    ) -> PruneCounts:
        """Delete at most ``limit`` rows per table, atomically."""
        ...


class PruneData:
    """Apply the retention policy in bounded batches."""

    def __init__(
        self,
        store: PruneStore,
        policy: RetentionPolicy,
        now: Callable[[], datetime],
    ) -> None:
        """Inject storage, policy and the clock."""
        self._store = store
        self._policy = policy
        self._now = now

    async def execute(self) -> tuple[PruneCounts, bool]:
        """Return the deleted rows and whether everything eligible went.

        ``False`` means ``max_batches`` was reached; the next scheduled
        run continues where this one stopped.
        """
        cutoffs = self._policy.cutoffs(self._now())
        total = PruneCounts()
        for _ in range(self._policy.max_batches):
            batch = await self._store.prune(cutoffs, self._policy.batch_size)
            total += batch
            if batch.total == 0:
                return total, True
        return total, False
