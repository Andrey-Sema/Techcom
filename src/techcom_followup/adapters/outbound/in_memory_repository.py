"""Reference repository with atomic writes and fenced leases."""

from asyncio import Lock
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import replace
from datetime import date, datetime
from heapq import nsmallest
from uuid import UUID, uuid4

from pydantic import JsonValue

from techcom_followup.application.repository import (
    ActionReadQuery,
    ActionRecord,
    ActionStatus,
    ActionUpdate,
    AuditEvent,
    CacheEntry,
    ClaimRequest,
    DecisionOutcome,
    DecisionWrite,
    RunRecord,
    StoredDecision,
    require_aware,
)


class InMemoryRepository:
    """Use a lock for writes and detach mutable snapshots."""

    def __init__(self) -> None:
        """Start with isolated per-instance storage."""
        self._lock = Lock()
        self._runs: dict[UUID, RunRecord] = {}
        self._decisions: dict[tuple[UUID, str], StoredDecision] = {}
        self._actions: dict[UUID, ActionRecord] = {}
        self._keys: set[str] = set()
        self._cache: dict[str, CacheEntry] = {}
        self._audit: list[AuditEvent] = []

    async def start_run(
        self, as_of: date, policy_version: str, now: datetime
    ) -> RunRecord:
        """Create an independent run using the supplied timestamp."""
        require_aware(now)
        run = RunRecord(uuid4(), as_of, policy_version, now, stats={})
        async with self._lock:
            self._runs[run.id] = run
        return deepcopy(run)

    async def finish_run(
        self, run_id: UUID, stats: Mapping[str, JsonValue], now: datetime
    ) -> bool:
        """Finish at most once and detach caller-owned statistics."""
        require_aware(now)
        async with self._lock:
            run = self._runs.get(run_id)
            if run is None or run.finished_at is not None:
                return False
            self._runs[run_id] = replace(
                run, finished_at=now, stats=deepcopy(dict(stats))
            )
            return True

    async def get_run(self, run_id: UUID) -> RunRecord | None:
        """Read a detached run snapshot."""
        return deepcopy(self._runs.get(run_id))

    async def record_decision(
        self, write: DecisionWrite, now: datetime
    ) -> StoredDecision:
        """Commit a decision, optional action and audit atomically."""
        write.validate()
        require_aware(now)
        async with self._lock:
            if write.run_id not in self._runs:
                message = "run does not exist"
                raise ValueError(message)
            key = (write.run_id, write.decision.task_id)
            previous = self._decisions.get(key)
            if previous is not None:
                return previous
            return self._insert(write, now, key)

    def _insert(
        self, write: DecisionWrite, now: datetime, key: tuple[UUID, str]
    ) -> StoredDecision:
        identity, draft = write.identity, write.message
        outcome = DecisionOutcome.NONE
        decision_id = uuid4()
        if identity is not None and draft is not None:
            outcome = DecisionOutcome.DUPLICATE_SUPPRESSED
            if identity.idempotency_key not in self._keys:
                outcome = DecisionOutcome.ENQUEUED
                action = ActionRecord(
                    uuid4(),
                    decision_id,
                    identity,
                    write.decision,
                    draft,
                    ActionStatus.PENDING,
                    0,
                    now,
                )
                self._actions[action.id] = action
                self._keys.add(identity.idempotency_key)
        stored = StoredDecision(decision_id, write, outcome)
        self._decisions[key] = stored
        self._audit.append(
            AuditEvent(
                f"decision.{outcome.value}",
                now,
                {"decision_id": str(decision_id)},
                write.run_id,
                write.decision.task_id,
            )
        )
        return stored

    async def list_decisions(self, run_id: UUID) -> tuple[StoredDecision, ...]:
        """Return this run's decisions in insertion order."""
        return tuple(
            row
            for row in self._decisions.values()
            if row.write.run_id == run_id
        )

    async def list_actions(self) -> tuple[ActionRecord, ...]:
        """Return immutable outbox snapshots in insertion order."""
        return tuple(self._actions.values())

    async def query_actions(
        self, query: ActionReadQuery
    ) -> tuple[ActionRecord, ...]:
        """Filter authority using at most ``limit`` working rows."""
        query.validate()
        visible = (
            action
            for action in self._actions.values()
            if _visible(action, query)
        )
        return tuple(nsmallest(query.limit, visible, key=lambda row: row.id))

    def _recover(self, now: datetime) -> None:
        for action in tuple(self._actions.values()):
            if (
                action.status == ActionStatus.IN_FLIGHT
                and action.lease_until is not None
                and action.lease_until <= now
            ):
                self._actions[action.id] = replace(
                    action,
                    status=ActionStatus.UNKNOWN,
                    lease_token=None,
                    lease_until=None,
                    next_attempt_at=now,
                    last_error="lease_expired",
                )

    async def claim_actions(
        self, request: ClaimRequest
    ) -> tuple[ActionRecord, ...]:
        """Claim pending sends, recovering expired sends to unknown."""
        return await self._claim(request, ActionStatus.PENDING)

    async def claim_unknown(
        self, request: ClaimRequest
    ) -> tuple[ActionRecord, ...]:
        """Claim unknown actions exclusively for reconciliation."""
        return await self._claim(request, ActionStatus.UNKNOWN)

    async def _claim(
        self, request: ClaimRequest, status: ActionStatus
    ) -> tuple[ActionRecord, ...]:
        request.validate()
        async with self._lock:
            self._recover(request.now)
            available = sorted(
                (
                    action
                    for action in self._actions.values()
                    if action.status == status
                    and action.next_attempt_at <= request.now
                    and (
                        action.lease_until is None
                        or action.lease_until <= request.now
                    )
                ),
                key=lambda action: (action.next_attempt_at, action.id),
            )[: request.limit]
            claimed = tuple(
                replace(
                    action,
                    status=(
                        ActionStatus.IN_FLIGHT
                        if status == ActionStatus.PENDING
                        else status
                    ),
                    attempts=action.attempts
                    + (status == ActionStatus.PENDING),
                    lease_token=uuid4(),
                    lease_until=request.now + request.lease,
                )
                for action in available
            )
            self._actions.update((action.id, action) for action in claimed)
            return claimed

    async def apply_outcome(self, update: ActionUpdate) -> bool:
        """Fence late workers, terminal rows and expired claims."""
        update.validate()
        async with self._lock:
            action = self._owned(
                update.action_id, update.lease_token, update.now
            )
            if action is None:
                return False
            self._actions[action.id] = replace(
                action,
                status=update.status,
                next_attempt_at=update.next_attempt_at,
                last_error=update.error,
                provider_ref=update.provider_ref,
                lease_token=None,
                lease_until=None,
                sent_at=update.now
                if update.status == ActionStatus.SENT
                else None,
            )
            return True

    def _owned(
        self, action_id: UUID, lease_token: UUID, now: datetime
    ) -> ActionRecord | None:
        action = self._actions.get(action_id)
        if (
            action is None
            or action.lease_token != lease_token
            or action.lease_until is None
            or action.lease_until <= now
            or action.status
            not in {ActionStatus.IN_FLIGHT, ActionStatus.UNKNOWN}
        ):
            return None
        return action

    async def defer_claim(self, update: ActionUpdate) -> bool:
        """Fence an unsent claim and restore its unspent attempt."""
        update.validate_defer()
        async with self._lock:
            action = self._owned(
                update.action_id, update.lease_token, update.now
            )
            if (
                action is None
                or action.status != ActionStatus.IN_FLIGHT
                or action.attempts < 1
            ):
                return False
            self._actions[action.id] = replace(
                action,
                status=ActionStatus.PENDING,
                attempts=action.attempts - 1,
                next_attempt_at=update.next_attempt_at,
                last_error=update.error,
                lease_token=None,
                lease_until=None,
            )
            return True

    async def release_claim(
        self, action_id: UUID, lease_token: UUID, now: datetime
    ) -> bool:
        """Cancellation is ambiguous: release into reconciliation."""
        return await self.apply_outcome(
            ActionUpdate(
                action_id, lease_token, now, ActionStatus.UNKNOWN, now
            )
        )

    async def put_cached_llm(self, entry: CacheEntry) -> None:
        """Store only bounded validated interpretations."""
        entry.validate()
        async with self._lock:
            self._cache[entry.key] = entry

    async def get_cached_llm(
        self, key: str, now: datetime
    ) -> CacheEntry | None:
        """Treat an entry as absent at and beyond its expiry."""
        require_aware(now)
        entry = self._cache.get(key)
        return entry if entry is not None and entry.expires_at > now else None

    async def append_audit(self, event: AuditEvent) -> None:
        """Append a detached event to the immutable audit trail."""
        require_aware(event.at)
        async with self._lock:
            self._audit.append(deepcopy(event))

    async def list_audit(self) -> tuple[AuditEvent, ...]:
        """Detach nested payloads from append-only storage."""
        return deepcopy(tuple(self._audit))


def _visible(action: ActionRecord, query: ActionReadQuery) -> bool:
    target = action.decision.target
    return (
        target is not None
        and (query.all_projects or target.project in query.projects)
        and (query.project is None or target.project == query.project)
        and (query.task_id is None or action.decision.task_id == query.task_id)
        and (query.status is None or action.status == query.status)
    )
