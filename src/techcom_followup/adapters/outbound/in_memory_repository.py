"""Reference repository with atomic writes and fenced leases."""

from asyncio import Lock
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import replace
from datetime import date, datetime
from heapq import nsmallest
from itertools import islice
from typing import cast
from uuid import UUID, uuid4

from pydantic import JsonValue

from techcom_followup.application.repository import (
    MAX_BATCH_SIZE,
    REVIEW_REJECTED,
    ActionReadQuery,
    ActionRecord,
    ActionStatus,
    ActionUpdate,
    AuditEvent,
    CacheEntry,
    ClaimRequest,
    DecisionOutcome,
    DecisionWrite,
    OutboxStatus,
    PruneCounts,
    RetentionCutoffs,
    RunRecord,
    StoredDecision,
    require_aware,
    review_event,
)
from techcom_followup.domain.messages import MessageDraft
from techcom_followup.domain.models import DecisionKind, Target

_TERMINAL = frozenset({ActionStatus.SENT, ActionStatus.DEAD})


class InMemoryRepository:
    """Use a lock for writes and detach mutable snapshots."""

    def __init__(self) -> None:
        """Start with isolated per-instance storage."""
        self._lock = Lock()
        self._runs: dict[UUID, RunRecord] = {}
        self._decisions: dict[tuple[UUID, str], StoredDecision] = {}
        self._actions: dict[UUID, ActionRecord] = {}
        self._superseded: set[UUID] = set()
        self._task_actions: dict[tuple[str, str], dict[UUID, str]] = {}
        self._keys: set[str] = set()
        self._notification_slots: set[tuple[str, str, str, str]] = set()
        self._messages: dict[str, MessageDraft] = {}
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
        self._supersede(write, now)
        outcome = DecisionOutcome.NONE
        decision_id = uuid4()
        if identity is not None and draft is not None:
            outcome = DecisionOutcome.DUPLICATE_SUPPRESSED
            slot = write.notification_slot()
            if (
                identity.idempotency_key not in self._keys
                and slot not in self._notification_slots
            ):
                outcome = DecisionOutcome.ENQUEUED
                action = ActionRecord(
                    uuid4(),
                    decision_id,
                    identity,
                    write.decision,
                    draft,
                    write.initial_status,
                    0,
                    now,
                    source_project=write.source_project,
                )
                self._actions[action.id] = action
                self._task_actions.setdefault(
                    write.notification_slot()[:2], {}
                )[action.id] = write.fingerprint
                self._keys.add(identity.idempotency_key)
                self._notification_slots.add(slot)
                self._messages[identity.idempotency_key] = draft
            else:
                # Keep aliases as tombstones too: a changed classifier
                # must not buy another model plan on every later run.
                self._keys.add(identity.idempotency_key)
        stored = StoredDecision(decision_id, write, outcome)
        self._decisions[key] = stored
        self._audit.append(
            AuditEvent(
                f"decision.{outcome.value}",
                now,
                write.audit_payload(decision_id),
                write.run_id,
                write.decision.task_id,
            )
        )
        return stored

    def _supersede(self, write: DecisionWrite, now: datetime) -> None:
        scope = write.notification_slot()[:2]
        for action_id, fingerprint in self._task_actions.get(
            scope, {}
        ).items():
            action = self._actions[action_id]
            obsolete = (
                fingerprint != write.fingerprint
                or write.decision.kind is DecisionKind.NO_ACTION
            )
            if obsolete and action.status not in _TERMINAL:
                self._superseded.add(action.id)
                if action.status in {ActionStatus.PENDING, ActionStatus.HELD}:
                    self._actions[action.id] = replace(
                        action,
                        status=ActionStatus.DEAD,
                        last_error="source_state_superseded",
                        terminal_at=now,
                    )

    def _fence_retry(
        self, action_id: UUID, update: ActionUpdate
    ) -> ActionUpdate:
        if (
            action_id in self._superseded
            and update.status is ActionStatus.PENDING
        ):
            return replace(
                update,
                status=ActionStatus.DEAD,
                error="source_state_superseded",
            )
        return update

    async def list_decisions(self, run_id: UUID) -> tuple[StoredDecision, ...]:
        """Order decisions by source position, keeping legacy ties."""
        rows = (
            row
            for row in self._decisions.values()
            if row.write.run_id == run_id
        )
        return tuple(
            sorted(
                rows,
                key=lambda row: (
                    row.write.source_position is None,
                    row.write.source_position or 0,
                ),
            )
        )

    async def list_actions(self) -> tuple[ActionRecord, ...]:
        """Return immutable outbox snapshots in insertion order."""
        return tuple(self._actions.values())

    async def list_run_actions(self, run_id: UUID) -> tuple[ActionRecord, ...]:
        """Avoid materializing other runs' payloads for a report."""
        order = {
            row.id: index
            for index, row in enumerate(await self.list_decisions(run_id))
        }
        return tuple(
            sorted(
                (
                    row
                    for row in self._actions.values()
                    if row.decision_id in order
                ),
                key=lambda row: order[row.decision_id],
            )
        )

    async def get_action_messages(
        self, keys: tuple[str, ...]
    ) -> Mapping[str, MessageDraft | None]:
        """Return payloads or tombstones without a history scan."""
        if len(keys) > MAX_BATCH_SIZE:
            message = "message lookup exceeds the batch bound"
            raise ValueError(message)
        return {
            key: self._messages.get(key) for key in keys if key in self._keys
        }

    async def query_actions(
        self, query: ActionReadQuery
    ) -> tuple[ActionRecord, ...]:
        """Filter authority using at most ``limit`` working rows."""
        query.validate()
        if query.newest_first:
            # Insertion order is creation order in this repository.
            latest = (
                action
                for action in reversed(self._actions.values())
                if _visible(action, query)
            )
            return tuple(islice(latest, query.limit))
        visible = (
            action
            for action in self._actions.values()
            if _visible(action, query)
        )
        return tuple(nsmallest(query.limit, visible, key=lambda row: row.id))

    async def review_held(
        self, operation_id: UUID, *, approve: bool, now: datetime
    ) -> ActionRecord | None:
        """Move one held action out of review with its audit event."""
        require_aware(now)
        async with self._lock:
            action = next(
                (
                    item
                    for item in self._actions.values()
                    if item.identity.operation_id == operation_id
                    and item.status is ActionStatus.HELD
                ),
                None,
            )
            if action is None:
                return None
            reviewed = replace(
                action,
                status=ActionStatus.PENDING if approve else ActionStatus.DEAD,
                next_attempt_at=now,
                last_error=None if approve else REVIEW_REJECTED,
                terminal_at=None if approve else now,
            )
            self._actions[action.id] = reviewed
            self._audit.append(
                review_event(reviewed, approve=approve, now=now)
            )
            return deepcopy(reviewed)

    async def outbox_summary(self) -> tuple[OutboxStatus, ...]:
        """Group actions by status with their earliest due time."""
        due: dict[ActionStatus, list[datetime]] = {}
        for action in self._actions.values():
            due.setdefault(action.status, []).append(action.next_attempt_at)
        return tuple(
            OutboxStatus(status, len(times), min(times))
            for status, times in sorted(due.items())
        )

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

    async def apply_outcome(self, update: ActionUpdate) -> ActionStatus | None:
        """Fence late workers, terminal rows and expired claims."""
        update.validate()
        async with self._lock:
            action = self._owned(
                update.action_id, update.lease_token, update.now
            )
            if action is None:
                return None
            update = self._fence_retry(action.id, update)
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
                terminal_at=update.now if update.status in _TERMINAL else None,
            )
            return update.status

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
            update = self._fence_retry(action.id, update)
            self._actions[action.id] = replace(
                action,
                status=update.status,
                attempts=action.attempts - 1,
                next_attempt_at=update.next_attempt_at,
                last_error=update.error,
                lease_token=None,
                lease_until=None,
                terminal_at=update.now if update.status in _TERMINAL else None,
            )
            return True

    async def release_claim(
        self, action_id: UUID, lease_token: UUID, now: datetime
    ) -> bool:
        """Cancellation is ambiguous: release into reconciliation."""
        return (
            await self.apply_outcome(
                ActionUpdate(
                    action_id, lease_token, now, ActionStatus.UNKNOWN, now
                )
            )
            is not None
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

    async def prune(
        self, cutoffs: RetentionCutoffs, limit: int
    ) -> PruneCounts:
        """Mirror the PostgreSQL retention rules in one locked batch."""
        async with self._lock:
            return PruneCounts(
                self._prune_cache(cutoffs.cache, limit),
                self._prune_actions(cutoffs.actions, limit),
                self._prune_decisions(cutoffs.decisions, limit),
                self._prune_audit(cutoffs.audit, limit),
                self._prune_runs(cutoffs.decisions, limit),
            )

    def _prune_cache(self, cutoff: datetime, limit: int) -> int:
        keys = [k for k, e in self._cache.items() if e.expires_at <= cutoff]
        for key in keys[:limit]:
            del self._cache[key]
        return len(keys[:limit])

    def _prune_actions(self, cutoff: datetime, limit: int) -> int:
        old = [
            action
            for action in self._actions.values()
            if action.status in _TERMINAL
            and action.terminal_at is not None
            and action.terminal_at < cutoff
        ][:limit]
        for action in old:
            del self._actions[action.id]
            self._superseded.discard(action.id)
            scope = (
                action.decision.task_id,
                action.source_project
                or cast("Target", action.decision.target).project
                or "",
            )
            del self._task_actions[scope][action.id]
            self._messages.pop(action.identity.idempotency_key, None)
            # Identity tombstones deliberately outlive payloads.
        return len(old)

    def _prune_decisions(self, cutoff: datetime, limit: int) -> int:
        referenced = {action.decision_id for action in self._actions.values()}
        old = [
            key
            for key, stored in self._decisions.items()
            if (finished := self._runs[key[0]].finished_at) is not None
            and finished < cutoff
            and stored.id not in referenced
        ][:limit]
        for key in old:
            del self._decisions[key]
        return len(old)

    def _prune_audit(self, cutoff: datetime, limit: int) -> int:
        old = [i for i, e in enumerate(self._audit) if e.at < cutoff][:limit]
        dropped = set(old)
        self._audit = [
            event
            for index, event in enumerate(self._audit)
            if index not in dropped
        ]
        return len(old)

    def _prune_runs(self, cutoff: datetime, limit: int) -> int:
        used = {run_id for run_id, _ in self._decisions}
        old = [
            run.id
            for run in self._runs.values()
            if run.finished_at is not None
            and run.finished_at < cutoff
            and run.id not in used
        ][:limit]
        for run_id in old:
            del self._runs[run_id]
        return len(old)


def _visible(action: ActionRecord, query: ActionReadQuery) -> bool:
    target = action.decision.target
    return (
        target is not None
        and _authorized({action.source_project, target.project}, query)
        and (
            query.project is None
            or {action.source_project, target.project} == {query.project}
        )
        and (query.task_id is None or action.decision.task_id == query.task_id)
        and (query.status is None or action.status == query.status)
    )


def _authorized(projects: set[str | None], query: ActionReadQuery) -> bool:
    return query.all_projects or projects <= set(query.projects)
