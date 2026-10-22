"""Single-attempt delivery with durable scheduling and fenced."""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from random import SystemRandom
from typing import Protocol

from techcom_followup.application.processing_ports import (
    Clock,
    DeliveryGateway,
    DeliveryKind,
    DeliveryResult,
)
from techcom_followup.application.repository import (
    ActionRecord,
    ActionStatus,
    ActionUpdate,
    ClaimRequest,
    Repository,
)
from techcom_followup.application.retry import RetryBudget, RetryPolicy

_RANDOM = SystemRandom()


@dataclass(frozen=True, slots=True)
class DeliveryDependencies:
    """Ports shared by the dispatcher and reconciler."""

    repository: Repository
    gateway: DeliveryGateway
    clock: Clock


class DeliveryBreaker(Protocol):
    """Admission and failure feedback, without an infrastructure."""

    def allow(self) -> bool:
        """Admit one call or half-open probe."""
        ...

    def record_success(self) -> None:
        """Close after healthy communication."""
        ...

    def record_failure(self) -> None:
        """Count consecutive dependency failures."""
        ...


class DispatchActions:
    """Claim one row at a time so stop switches never strand a batch."""

    def __init__(
        self,
        dependencies: DeliveryDependencies,
        *,
        enabled: Callable[[], bool] = lambda: True,
        retry: RetryPolicy | None = None,
        jitter: Callable[[], float] = _RANDOM.random,
        breaker: DeliveryBreaker | None = None,
    ) -> None:
        """Inject live kill switch, bounded retry policy and breaker."""
        self._deps = dependencies
        self._enabled = enabled
        self._retry = retry if retry is not None else RetryPolicy()
        self._jitter = jitter
        self._breaker = breaker
        self._stopping = False

    def stop(self) -> None:
        """Stop future claims while allowing the current send to."""
        self._stopping = True

    async def execute(
        self, *, limit: int = 50, budget: RetryBudget | None = None
    ) -> int:
        """Process a bounded cycle; ambiguous cancellations become."""
        ClaimRequest(self._deps.clock.now(), limit=limit).validate()
        admitted = budget if budget is not None else RetryBudget(limit)
        completed = 0
        for _ in range(limit):
            if self._stopping or not self._enabled():
                break
            claimed = await self._deps.repository.claim_actions(
                ClaimRequest(self._deps.clock.now(), limit=1)
            )
            if not claimed:
                break
            action = claimed[0]
            if action.attempts > 1 and not admitted.take():
                await self._defer(action, "retry_budget")
                break
            if self._breaker is not None and not self._breaker.allow():
                await self._defer(action, "circuit_open")
                break
            await self._deliver(action)
            completed += 1
        return completed

    async def _defer(self, action: ActionRecord, error: str) -> None:
        if action.lease_token is None:
            message = "repository returned an unfenced action"
            raise ValueError(message)
        now = self._deps.clock.now()
        await self._deps.repository.defer_claim(
            ActionUpdate(
                action.id,
                action.lease_token,
                now,
                ActionStatus.PENDING,
                now + timedelta(seconds=self._retry.base_seconds),
                error=error,
            )
        )

    async def _deliver(self, action: ActionRecord) -> None:
        try:
            result = await self._deps.gateway.send(action)
            if self._breaker is not None:
                if result.kind in {
                    DeliveryKind.UNKNOWN,
                    DeliveryKind.RETRYABLE,
                }:
                    self._breaker.record_failure()
                else:
                    self._breaker.record_success()
            await self._apply(action, result)
        except BaseException:
            await asyncio.shield(self._release(action))
            raise

    async def _release(self, action: ActionRecord) -> None:
        if action.lease_token is not None:
            await self._deps.repository.release_claim(
                action.id, action.lease_token, self._deps.clock.now()
            )

    async def _apply(
        self, action: ActionRecord, result: DeliveryResult
    ) -> None:
        now = self._deps.clock.now()
        status = delivery_status(result, action.attempts, self._retry)
        delay = self._retry.delay(
            action.attempts,
            jitter=self._jitter(),
            retry_after=result.retry_after,
        )
        if action.lease_token is None:
            message = "repository returned an unfenced action"
            raise ValueError(message)
        await self._deps.repository.apply_outcome(
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


def delivery_status(
    result: DeliveryResult, attempts: int, retry: RetryPolicy
) -> ActionStatus:
    """Unknown outcomes retain reconciliation even at the attempt."""
    if result.kind is DeliveryKind.ACCEPTED:
        return ActionStatus.SENT
    if result.kind in {DeliveryKind.UNKNOWN, DeliveryKind.ABSENT}:
        return ActionStatus.UNKNOWN
    if result.kind is DeliveryKind.PERMANENT or attempts >= retry.max_attempts:
        return ActionStatus.DEAD
    return ActionStatus.PENDING
