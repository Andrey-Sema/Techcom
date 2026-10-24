"""Persistence port and immutable outbox snapshots.

A claim is fenced by its random token and expiry. Releasing a delivery
claim makes its outcome unknown: cancellation cannot prove no send took
place. Only reconciliation can make unknown work dispatchable again.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import StrEnum
from math import isfinite
from typing import Literal, Protocol
from uuid import UUID

from pydantic import JsonValue

from techcom_followup.domain.identity import ActionIdentity
from techcom_followup.domain.messages import MessageDraft
from techcom_followup.domain.models import (
    Decision,
    DecisionKind,
    Flag,
    NoteInterpretation,
)
from techcom_followup.domain.source_version import SourceVersion

MAX_BATCH_SIZE = 1000
MAX_ERROR_LENGTH = 200
MAX_EVIDENCE_LENGTH = 200
MAX_ACTION_READ_LIMIT = 100
MAX_READ_PROJECTS = 128
MAX_PROJECT_LENGTH = 200
REVIEW_REJECTED = "rejected_in_review"
_UNSTORABLE_TEXT = re.compile(r"[\x00\ud800-\udfff]")


class ActionStatus(StrEnum):
    """Durable delivery states; sent and dead are terminal.

    ``held`` waits for a person: only an operator review moves it to
    ``pending`` (approve) or ``dead`` (reject). Dispatchers never claim
    it, so a held action cannot be sent by any automated path.
    """

    PENDING = "pending"
    IN_FLIGHT = "in_flight"
    UNKNOWN = "unknown"
    SENT = "sent"
    DEAD = "dead"
    HELD = "held"


class DecisionOutcome(StrEnum):
    """Outcome of an atomic decision and outbox write."""

    NONE = "none"
    ENQUEUED = "enqueued"
    DUPLICATE_SUPPRESSED = "duplicate_suppressed"
    STALE_SUPPRESSED = "stale_suppressed"


@dataclass(frozen=True, slots=True)
class DecisionWrite:
    """Facts required for the one-transaction decision/outbox write."""

    run_id: UUID
    decision: Decision
    fingerprint: str
    identity: ActionIdentity | None = None
    message: MessageDraft | None = None
    source_project: str | None = None
    source_position: int | None = None
    baseline: Decision | None = None
    plan_audit: Mapping[str, JsonValue] | None = None
    source_version: SourceVersion | None = None
    # Display label; scopes decisions that have no recipient project.
    task_project: str | None = None

    def audit_payload(self, decision_id: UUID) -> dict[str, JsonValue]:
        """Return the decision receipt, with the plan trace if any."""
        payload: dict[str, JsonValue] = {"decision_id": str(decision_id)}
        if self.plan_audit is not None:
            payload["plan"] = dict(self.plan_audit)
        return payload

    @property
    def initial_status(self) -> ActionStatus:
        """Held for a person when the planner asked for a review."""
        if Flag.HELD_FOR_REVIEW in self.decision.flags:
            return ActionStatus.HELD
        return ActionStatus.PENDING

    def notification_slot(self) -> tuple[str, str, str, str]:
        """Return the durable slot one source state may notify once.

        A classifier or model change for the same state cannot buy a
        second message, but an unchanged task that crosses the
        escalation threshold still reaches the manager once: the
        policy baseline decides the stage, not the model.
        """
        target = self.decision.target
        project = (
            self.source_project
            or (target.project if target is not None else None)
            or self.task_project
        )
        slot = self.decision if self.baseline is None else self.baseline
        stage = "escalate" if slot.kind is DecisionKind.ESCALATE else "notify"
        return (self.decision.task_id, project or "", self.fingerprint, stage)

    def validate(self) -> None:
        """Reject partial actions or a forged operation identity.

        A planned action keeps the identity of its policy ``baseline``:
        the slot belongs to the task state, so a model choosing another
        allowed action on a rerun cannot create a second outbound
        message for the same state.
        """
        self._validate_source()
        actionable = self.decision.actionable
        if actionable != (
            self.identity is not None and self.message is not None
        ):
            message = "actionable decisions need identity and message"
            raise ValueError(message)
        if not actionable and (self.identity or self.message):
            message = "quiet decisions cannot carry an action"
            raise ValueError(message)
        self._validate_slot(actionable=actionable)

    def _validate_slot(self, *, actionable: bool) -> None:
        slot = self.decision if self.baseline is None else self.baseline
        if actionable and self.identity != ActionIdentity.of(
            slot, self.fingerprint
        ):
            message = "action identity does not match decision and state"
            raise ValueError(message)
        if self.baseline is not None and (
            self.baseline.task_id != self.decision.task_id
            or not self.baseline.actionable
            or not actionable
        ):
            message = "a planned action must refine an actionable baseline"
            raise ValueError(message)

    def _validate_source(self) -> None:
        if self.task_project is not None and (
            not 1
            <= len(require_storage_text(self.task_project))
            <= MAX_PROJECT_LENGTH
        ):
            message = "invalid task project"
            raise ValueError(message)
        if self.source_project is not None:
            require_storage_text(self.source_project)
            if not 1 <= len(self.source_project) <= MAX_PROJECT_LENGTH:
                message = "invalid source project"
                raise ValueError(message)
            target = self.decision.target
            if target is not None and target.project != self.source_project:
                message = "recipient project differs from source"
                raise ValueError(message)
        if self.source_position is not None and (
            type(self.source_position) is not int or self.source_position < 0
        ):
            message = "invalid source position"
            raise ValueError(message)


@dataclass(frozen=True, slots=True)
class StoredDecision:
    """A per-run decision receipt, returned unchanged on task replay."""

    id: UUID
    write: DecisionWrite
    outcome: DecisionOutcome


@dataclass(frozen=True, slots=True)
class ActionRecord:
    """A detached row; mutations require a valid fencing token."""

    id: UUID
    decision_id: UUID
    identity: ActionIdentity
    decision: Decision
    message: MessageDraft
    status: ActionStatus
    attempts: int
    next_attempt_at: datetime
    lease_token: UUID | None = None
    lease_until: datetime | None = None
    last_error: str | None = None
    provider_ref: str | None = None
    sent_at: datetime | None = None
    terminal_at: datetime | None = None
    source_project: str | None = None


@dataclass(frozen=True, slots=True)
class OutboxStatus:
    """Outbox rows in one status and their earliest due time."""

    status: ActionStatus
    count: int
    earliest_due: datetime | None


@dataclass(frozen=True, slots=True)
class ActionReadQuery:
    """Filter server-authorized actions before the row bound.

    Projects come only from verified authority, never request filters.
    Empty projects deny access unless all-project access is explicit.
    Project, task and status filters can only narrow that authority.
    """

    projects: frozenset[str] = frozenset()
    all_projects: bool = False
    project: str | None = None
    task_id: str | None = None
    status: ActionStatus | None = None
    limit: int = MAX_ACTION_READ_LIMIT
    newest_first: bool = False

    def validate(self) -> None:
        """Reject unbounded reads and ambiguous project authority."""
        if type(self.limit) is not int or not (
            1 <= self.limit <= MAX_ACTION_READ_LIMIT
        ):
            message = "action read limit must be an integer in 1..100"
            raise ValueError(message)
        _require_project_authority(self.projects, self.all_projects)
        for value in (*self.projects, self.project, self.task_id):
            if value is not None:
                require_storage_text(value)


def _require_project_authority(projects: object, all_projects: object) -> None:
    if (
        not isinstance(projects, frozenset)
        or type(all_projects) is not bool
        or len(projects) > MAX_READ_PROJECTS
        or any(
            not isinstance(value, str)
            or not value
            or len(value) > MAX_PROJECT_LENGTH
            for value in projects
        )
        or (all_projects is True and bool(projects))
    ):
        message = "action read project authority is invalid"
        raise ValueError(message)


def require_storage_text(value: str) -> str:
    """Reject text that PostgreSQL cannot represent without alteration.

    Raises:
        ValueError: A NUL or isolated Unicode surrogate is present.
    """
    if _UNSTORABLE_TEXT.search(value):
        message = "text must be UTF-8 without NUL or isolated surrogates"
        raise ValueError(message)
    return value


@dataclass(frozen=True, slots=True)
class ClaimRequest:
    """Bounded batch request using an injected, aware UTC clock."""

    now: datetime
    limit: int = 50
    lease: timedelta = timedelta(seconds=30)

    def validate(self) -> None:
        """Reject unbounded batches and expired or naive leases."""
        require_aware(self.now)
        if not 1 <= self.limit <= MAX_BATCH_SIZE or self.lease <= timedelta(0):
            message = "claim needs limit 1..1000 and a positive lease"
            raise ValueError(message)


@dataclass(frozen=True, slots=True)
class ActionUpdate:
    """An outcome applied only while its claim owns a row."""

    action_id: UUID
    lease_token: UUID
    now: datetime
    status: ActionStatus
    next_attempt_at: datetime
    error: str | None = None
    provider_ref: str | None = None

    def validate(self) -> None:
        """Keep scheduling dates aware and audit errors bounded."""
        require_aware(self.now)
        require_aware(self.next_attempt_at)
        if self.status == ActionStatus.IN_FLIGHT:
            message = "claiming is the only way to enter in_flight"
            raise ValueError(message)
        if self.error is not None and len(self.error) > MAX_ERROR_LENGTH:
            message = "errors must be closed codes of at most 200 characters"
            raise ValueError(message)

    def validate_defer(self) -> None:
        """Require closed operational codes for unsent deferrals."""
        self.validate()
        if self.status != ActionStatus.PENDING:
            message = "unsent claims can only be deferred to pending"
            raise ValueError(message)
        if self.error not in {
            None,
            "circuit_open",
            "retry_budget",
            "dispatch_stopped",
            "recipient_limit",
        }:
            message = "unsent deferrals require a closed error code"
            raise ValueError(message)


@dataclass(frozen=True, slots=True)
class RunRecord:
    """Run lifecycle facts and aggregate statistics."""

    id: UUID
    as_of: date
    policy_version: str
    started_at: datetime
    finished_at: datetime | None = None
    stats: Mapping[str, JsonValue] | None = None


@dataclass(frozen=True, slots=True)
class AuditEvent:
    """Append-only metadata; payloads must omit raw notes."""

    kind: str
    at: datetime
    payload: Mapping[str, JsonValue]
    run_id: UUID | None = None
    task_id: str | None = None


type DeliveryAuditKind = Literal["delivery", "reconciliation"]


def outcome_event(
    action: ActionRecord,
    now: datetime,
    status: ActionStatus,
    kind: DeliveryAuditKind,
) -> AuditEvent:
    """Build the receipt from the effective fenced storage outcome."""
    name = (
        f"delivery.{status.value}"
        if kind == "delivery"
        else "delivery.reconciled"
        if status is ActionStatus.SENT
        else "delivery.reconciliation"
    )
    return AuditEvent(
        name,
        now,
        {
            "action_id": str(action.id),
            "operation_id": str(action.identity.operation_id),
            "attempt": action.attempts,
            "status": status.value,
        },
        task_id=action.decision.task_id,
    )


@dataclass(frozen=True, slots=True)
class CacheEntry:
    """A validated interpretation with explicit expiry."""

    key: str
    model: str
    prompt_version: str
    interpretation: NoteInterpretation
    created_at: datetime
    expires_at: datetime

    def validate(self) -> None:
        """Reject cache entries without a finite, aware lifetime."""
        confidence = self.interpretation.confidence
        if (
            not isfinite(confidence)
            or not 0 <= confidence <= 1
            or len(self.interpretation.evidence) > MAX_EVIDENCE_LENGTH
        ):
            message = "cache interpretation is outside validated bounds"
            raise ValueError(message)
        require_aware(self.created_at)
        require_aware(self.expires_at)
        if self.expires_at <= self.created_at:
            message = "cache expiry must follow creation"
            raise ValueError(message)


@dataclass(frozen=True, slots=True)
class RetentionCutoffs:
    """Rows strictly older than these instants are eligible."""

    cache: datetime
    decisions: datetime
    actions: datetime
    audit: datetime


@dataclass(frozen=True, slots=True)
class PruneCounts:
    """Rows deleted per table in one batch or in total."""

    cache: int = 0
    actions: int = 0
    decisions: int = 0
    audit: int = 0
    runs: int = 0
    admissions: int = 0

    @property
    def total(self) -> int:
        """All deleted rows."""
        return (
            self.cache
            + self.actions
            + self.decisions
            + self.audit
            + self.runs
            + self.admissions
        )

    def __add__(self, other: "PruneCounts") -> "PruneCounts":
        """Sum two batches."""
        return PruneCounts(
            self.cache + other.cache,
            self.actions + other.actions,
            self.decisions + other.decisions,
            self.audit + other.audit,
            self.runs + other.runs,
            self.admissions + other.admissions,
        )


def review_event(
    action: ActionRecord, *, approve: bool, now: datetime
) -> AuditEvent:
    """Describe an operator review without any message content."""
    return AuditEvent(
        "action.review_approved" if approve else "action.review_rejected",
        now,
        {
            "action_id": str(action.id),
            "operation_id": str(action.identity.operation_id),
        },
        task_id=action.decision.task_id,
    )


def require_aware(value: datetime) -> None:
    """Reject ambiguous local datetimes at persistence boundaries."""
    if value.tzinfo is None or value.utcoffset() is None:
        message = "timestamp must be timezone-aware"
        raise ValueError(message)


class Repository(Protocol):
    """Atomic writes and fenced claims independent of storage."""

    async def start_run(
        self, as_of: date, policy_version: str, now: datetime
    ) -> RunRecord:
        """Create a new run before any per-task writes."""
        ...

    async def finish_run(
        self, run_id: UUID, stats: Mapping[str, JsonValue], now: datetime
    ) -> bool:
        """Finish once; false means missing or already finished."""
        ...

    async def get_run(self, run_id: UUID) -> RunRecord | None:
        """Read a detached run snapshot."""
        ...

    async def record_decision(
        self, write: DecisionWrite, now: datetime
    ) -> StoredDecision:
        """Persist decision, optional action and audit atomically."""
        ...

    async def list_decisions(self, run_id: UUID) -> tuple[StoredDecision, ...]:
        """Read run decisions in stable insertion order."""
        ...

    async def get_action_messages(
        self, keys: tuple[str, ...]
    ) -> Mapping[str, MessageDraft | None]:
        """Read at most 1000 identities for trusted global processing.

        A present key with no message is a retained identity tombstone.
        This is not an end-user read port; use authorized action
        queries.
        """
        ...

    async def list_run_actions(self, run_id: UUID) -> tuple[ActionRecord, ...]:
        """Read actions created by one run, in stable source order."""
        ...

    async def list_actions(self) -> tuple[ActionRecord, ...]:
        """Read detached snapshots for reports and reconciliation."""
        ...

    async def outbox_summary(self) -> tuple[OutboxStatus, ...]:
        """Count actions per status for backlog and latency metrics."""
        ...

    async def query_actions(
        self, query: ActionReadQuery
    ) -> tuple[ActionRecord, ...]:
        """Filter authority before a stable UUID-ordered row limit."""
        ...

    async def review_held(
        self, operation_id: UUID, *, approve: bool, now: datetime
    ) -> ActionRecord | None:
        """Approve a held action to pending, or reject it as dead.

        Only a ``held`` row changes, together with an audit event in
        the same transaction; anything else returns ``None``.
        """
        ...

    async def claim_actions(
        self, request: ClaimRequest
    ) -> tuple[ActionRecord, ...]:
        """Claim pending rows; expired sends become unknown first."""
        ...

    async def claim_unknown(
        self, request: ClaimRequest
    ) -> tuple[ActionRecord, ...]:
        """Fence unknown rows for lookup without counting attempts."""
        ...

    async def apply_outcome(
        self,
        update: ActionUpdate,
        *,
        audit_kind: DeliveryAuditKind | None = None,
    ) -> ActionStatus | None:
        """Commit the fenced status and optional receipt atomically."""
        ...

    async def get_action_plans(
        self, keys: tuple[str, ...]
    ) -> Mapping[str, DecisionWrite | None]:
        """Read complete retained plans or identity-only tombstones."""
        ...

    async def defer_claim(self, update: ActionUpdate) -> bool:
        """Return an unsent claim to pending without an attempt.

        Call only before starting any delivery I/O. Unknown outcomes
        cannot use this path and require lookup before becoming pending.
        """
        ...

    async def release_claim(
        self, action_id: UUID, lease_token: UUID, now: datetime
    ) -> bool:
        """Release work to unknown without counting an attempt."""
        ...

    async def put_cached_llm(self, entry: CacheEntry) -> None:
        """Upsert only a validated, time-limited interpretation."""
        ...

    async def get_cached_llm(
        self, key: str, now: datetime
    ) -> CacheEntry | None:
        """Treat an entry as absent at and beyond its expiry."""
        ...

    async def append_audit(self, event: AuditEvent) -> None:
        """Append metadata without an update/delete method."""
        ...

    async def list_audit(self) -> tuple[AuditEvent, ...]:
        """Read detached audit snapshots."""
        ...

    async def prune(
        self, cutoffs: RetentionCutoffs, limit: int
    ) -> PruneCounts:
        """Delete at most ``limit`` eligible rows per table, atomically.

        Never deletes pending, in-flight or unknown actions, a decision
        an action still refers to, or a run that still has decisions.
        """
        ...
