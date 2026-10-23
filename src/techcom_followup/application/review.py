"""Operator review of actions the planner held for a person.

A held action is never claimed by a dispatcher. Only this use case
moves it on: approve makes it ``pending`` (the next dispatch sends it
with its original operation identity), reject makes it ``dead``. The
model has no tool that reaches this code.
"""

from uuid import UUID

from techcom_followup.application.processing_ports import Clock
from techcom_followup.application.repository import (
    MAX_ACTION_READ_LIMIT,
    ActionReadQuery,
    ActionRecord,
    ActionStatus,
    Repository,
)
from techcom_followup.application.telemetry import (
    NULL_TELEMETRY,
    CounterName,
    Telemetry,
)


class ReviewHeld:
    """List, approve or reject held actions."""

    def __init__(
        self,
        repository: Repository,
        clock: Clock,
        telemetry: Telemetry = NULL_TELEMETRY,
    ) -> None:
        """Inject storage, the UTC clock and metrics."""
        self._repository = repository
        self._clock = clock
        self._telemetry = telemetry

    async def held(
        self, *, limit: int = MAX_ACTION_READ_LIMIT
    ) -> tuple[ActionRecord, ...]:
        """Return the oldest held actions, across all projects."""
        return await self._repository.query_actions(
            ActionReadQuery(
                all_projects=True, status=ActionStatus.HELD, limit=limit
            )
        )

    async def decide(
        self, operation_id: UUID, *, approve: bool
    ) -> ActionRecord | None:
        """Apply one decision; ``None`` if the action is not held."""
        reviewed = await self._repository.review_held(
            operation_id, approve=approve, now=self._clock.now()
        )
        if reviewed is not None:
            self._telemetry.count(
                CounterName.ACTIONS,
                outcome="approved" if approve else "rejected",
            )
        return reviewed
