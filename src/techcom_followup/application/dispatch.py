"""Durable delivery scheduling with fenced outcomes."""

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
from techcom_followup.application.resilience_ports import BreakerPermit
from techcom_followup.application.retry import RetryBudget, RetryPolicy

_RANDOM = SystemRandom()


@dataclass(frozen=True, slots=True)
class DeliveryDependencies:
    """Ports shared by the dispatcher and reconciler."""

    repository: Repository
    gateway: DeliveryGateway
    clock: Clock


class DeliveryBreaker(Protocol):
    """Dependency health feedback and probe admission."""

    @property
    def state(self) -> str:
        """Expose closed/open/half_open without reserving a probe."""
        ...

    def admit(self) -> BreakerPermit | None:
        """Reserve a call or half-open probe with attempt ownership."""
        ...

    def record_success(self, permit: BreakerPermit | None = None) -> None:
        """Close after healthy communication."""
        ...

    def record_failure(self, permit: BreakerPermit | None = None) -> None:
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
        """Stop claims and let the current send finish."""
        self._stopping = True

    def _can_claim(self) -> bool:
        return not self._stopping and self._enabled()

    async def execute(
        self, *, limit: int = 50, budget: RetryBudget | None = None
    ) -> int:
        """Process a bounded cycle and reconcile cancellations."""
        ClaimRequest(self._deps.clock.now(), limit=limit).validate()
        admitted = budget if budget is not None else RetryBudget(limit)
        completed = 0
        for _ in range(limit):
            if not self._can_claim():
                break
            if self._breaker is not None and self._breaker.state == "open":
                break
            claimed = await self._deps.repository.claim_actions(
                ClaimRequest(self._deps.clock.now(), limit=1)
            )
            if not claimed:
                break
            action = claimed[0]
            if not self._can_claim():
                await self._defer(action, "dispatch_stopped")
                break
            if action.attempts > 1 and not admitted.take():
                await self._defer(action, "retry_budget")
                break
            allowed, permit = self._admit()
            if not allowed:
                await self._defer(action, "circuit_open")
                break
            await self._deliver(action, permit)
            completed += 1
        return completed

    def _admit(self) -> tuple[bool, BreakerPermit | None]:
        if self._breaker is None:
            return True, None
        permit = self._breaker.admit()
        return permit is not None, permit

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

    async def _deliver(
        self, action: ActionRecord, permit: BreakerPermit | None
    ) -> None:
        try:
            try:
                result = await self._deps.gateway.send(action)
            except BaseException:
                if self._breaker is not None:
                    self._breaker.record_failure(permit)
                raise
            if self._breaker is not None:
                if result.kind in {
                    DeliveryKind.UNKNOWN,
                    DeliveryKind.RETRYABLE,
                }:
                    self._breaker.record_failure(permit)
                else:
                    self._breaker.record_success(permit)
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
        if action.lease_token is None:
            message = "repository returned an unfenced action"
            raise ValueError(message)
        now = self._deps.clock.now()
        status = delivery_status(result, action.attempts, self._retry)
        delay = self._retry.delay(
            action.attempts,
            jitter=self._jitter(),
            retry_after=result.retry_after,
        )
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
    """Reconcile unknown outcomes even at the attempt cap."""
    if result.kind is DeliveryKind.ACCEPTED:
        return ActionStatus.SENT
    if result.kind in {DeliveryKind.UNKNOWN, DeliveryKind.ABSENT}:
        return ActionStatus.UNKNOWN
    if result.kind is DeliveryKind.PERMANENT or attempts >= retry.max_attempts:
        return ActionStatus.DEAD
    return ActionStatus.PENDING
