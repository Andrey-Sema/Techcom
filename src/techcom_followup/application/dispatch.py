"""Durable delivery scheduling with fenced outcomes."""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from random import SystemRandom
from typing import Protocol

from techcom_followup.application.delivery_audit import audit_delivery
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
from techcom_followup.application.telemetry import (
    NULL_TELEMETRY,
    CounterName,
    Telemetry,
)

_RANDOM = SystemRandom()


@dataclass(frozen=True, slots=True)
class DeliveryDependencies:
    """Ports shared by the dispatcher and reconciler."""

    repository: Repository
    gateway: DeliveryGateway
    clock: Clock
    telemetry: Telemetry = NULL_TELEMETRY


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

    def abandon(self, permit: BreakerPermit) -> None:
        """Release only an attempt's own abandoned probe."""
        ...


class DeliveryQuota(Protocol):
    """Durable admission before delivery, shared across dispatchers."""

    async def allow(self, action: ActionRecord, now: datetime) -> bool:
        """Reserve an operation once per recipient and UTC day."""
        ...


@dataclass(frozen=True, slots=True)
class _Admission:
    breaker: DeliveryBreaker
    permit: BreakerPermit


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
        quota: DeliveryQuota | None = None,
    ) -> None:
        """Inject live kill switch, bounded retry policy and breaker."""
        self._deps = dependencies
        self._enabled = enabled
        self._retry = retry if retry is not None else RetryPolicy()
        self._jitter = jitter
        self._breaker = breaker
        self._quota = quota
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
            attempted = await self._dispatch_claim(claimed[0], admitted)
            if attempted is None:
                break
            completed += int(attempted)
        return completed

    async def _dispatch_claim(
        self, action: ActionRecord, budget: RetryBudget
    ) -> bool | None:
        if not self._can_claim():
            await self._defer(action, "dispatch_stopped")
            return None
        if action.attempts > 1 and not budget.take():
            await self._defer(action, "retry_budget")
            return None
        allowed, admission = self._admit()
        if not allowed:
            await self._defer(action, "circuit_open")
            return None
        reserved = await self._reserve_quota(action, admission)
        if reserved:
            await self._deliver(action, admission)
        return reserved

    async def _reserve_quota(
        self, action: ActionRecord, admission: _Admission | None
    ) -> bool:
        if self._quota is None:
            return True
        try:
            reserved = await self._quota.allow(action, self._deps.clock.now())
        except BaseException:
            if admission is not None:
                admission.breaker.abandon(admission.permit)
            await asyncio.shield(self._release(action))
            raise
        if reserved:
            return True
        if admission is not None:
            admission.breaker.abandon(admission.permit)
        await self._defer(action, "recipient_limit")
        return False

    def _admit(self) -> tuple[bool, _Admission | None]:
        if self._breaker is None:
            return True, None
        permit = self._breaker.admit()
        if permit is None:
            return False, None
        return True, _Admission(self._breaker, permit)

    async def _defer(self, action: ActionRecord, error: str) -> None:
        if action.lease_token is None:
            message = "repository returned an unfenced action"
            raise ValueError(message)
        now = self._deps.clock.now()
        next_attempt = now + timedelta(seconds=self._retry.base_seconds)
        if error == "recipient_limit":
            tomorrow = now.astimezone(UTC).date() + timedelta(days=1)
            next_attempt = datetime.combine(tomorrow, datetime.min.time(), UTC)
        await self._deps.repository.defer_claim(
            ActionUpdate(
                action.id,
                action.lease_token,
                now,
                ActionStatus.PENDING,
                next_attempt,
                error=error,
            )
        )

    async def _deliver(
        self, action: ActionRecord, admission: _Admission | None
    ) -> None:
        try:
            await audit_delivery(
                self._deps.repository,
                action,
                self._deps.clock.now(),
                "delivery.attempt",
                action.status,
            )
            try:
                result = await self._deps.gateway.send(action)
            except BaseException:
                if admission is not None:
                    admission.breaker.record_failure(admission.permit)
                raise
            self._deps.telemetry.count(
                CounterName.DELIVERY_ATTEMPTS, result=result.kind.value
            )
            if admission is not None:
                if result.kind in {
                    DeliveryKind.UNKNOWN,
                    DeliveryKind.RETRYABLE,
                }:
                    admission.breaker.record_failure(admission.permit)
                else:
                    admission.breaker.record_success(admission.permit)
            await self._apply(action, result)
        except BaseException:
            await asyncio.shield(self._release(action))
            raise
        finally:
            if admission is not None:
                admission.breaker.abandon(admission.permit)

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
        if applied:
            if status is not ActionStatus.PENDING:
                self._deps.telemetry.count(
                    CounterName.ACTIONS, outcome=status.value
                )
            await audit_delivery(
                self._deps.repository,
                action,
                now,
                f"delivery.{status.value}",
                status,
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
