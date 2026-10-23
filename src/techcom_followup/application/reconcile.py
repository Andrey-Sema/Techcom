"""Reconcile uncertain delivery before any resend is allowed."""

import asyncio
from datetime import timedelta

from techcom_followup.application.delivery_audit import audit_delivery
from techcom_followup.application.dispatch import DeliveryDependencies
from techcom_followup.application.processing_ports import DeliveryKind
from techcom_followup.application.repository import (
    ActionRecord,
    ActionStatus,
    ActionUpdate,
    ClaimRequest,
)
from techcom_followup.application.retry import RetryPolicy
from techcom_followup.application.telemetry import CounterName


class ReconcileUnknown:
    """Only a validated lookup 404 authorizes an unknown resend."""

    def __init__(
        self,
        dependencies: DeliveryDependencies,
        *,
        retry: RetryPolicy | None = None,
    ) -> None:
        """Inject storage, single-attempt lookup and UTC clock."""
        self._deps = dependencies
        self._retry = retry if retry is not None else RetryPolicy()

    async def execute(self, *, limit: int = 50) -> int:
        """Reconcile a bounded batch with fenced updates."""
        ClaimRequest(self._deps.clock.now(), limit=limit).validate()
        completed = 0
        for _ in range(limit):
            rows = await self._deps.repository.claim_unknown(
                ClaimRequest(self._deps.clock.now(), limit=1)
            )
            if not rows:
                break
            await self._reconcile(rows[0])
            completed += 1
        return completed

    def _count(self, status: ActionStatus) -> None:
        outcome = {
            ActionStatus.SENT: "reconciled",
            ActionStatus.DEAD: "dead",
        }.get(status)
        if outcome is not None:
            self._deps.telemetry.count(CounterName.ACTIONS, outcome=outcome)

    async def _reconcile(self, action: ActionRecord) -> None:
        if action.lease_token is None:
            message = "repository returned an unfenced unknown action"
            raise ValueError(message)
        try:
            result = await self._deps.gateway.lookup(
                action.identity.operation_id
            )
            status = ActionStatus.UNKNOWN
            if result.kind is DeliveryKind.ACCEPTED:
                status = ActionStatus.SENT
            elif result.kind is DeliveryKind.ABSENT:
                status = (
                    ActionStatus.DEAD
                    if action.attempts >= self._retry.max_attempts
                    else ActionStatus.PENDING
                )
            now = self._deps.clock.now()
            delay = self._retry.delay(
                max(1, action.attempts),
                jitter=1,
                retry_after=result.retry_after,
            )
            applied = await self._deps.repository.apply_outcome(
                ActionUpdate(
                    action.id,
                    action.lease_token,
                    now,
                    status,
                    now + timedelta(seconds=delay),
                    error=result.error,
                    provider_ref=result.provider_ref,
                )
            )
            if applied is not None:
                status = applied
                self._count(status)
                await audit_delivery(
                    self._deps.repository,
                    action,
                    now,
                    "delivery.reconciled"
                    if status is ActionStatus.SENT
                    else "delivery.reconciliation",
                    status,
                )
        except BaseException:
            await asyncio.shield(
                self._deps.repository.release_claim(
                    action.id, action.lease_token, self._deps.clock.now()
                )
            )
            raise
