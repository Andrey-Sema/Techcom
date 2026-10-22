"""Domain vocabulary: enums and immutable value objects.

Every value object is a frozen, slotted dataclass. Nothing here talks
to the outside world; the only way task data enters the domain is
through ``normalize``.
"""

from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum
from typing import NewType, Self

from techcom_followup.domain.text import clean_block

TaskId = NewType("TaskId", str)

MAX_NOTE_CHARS = 2_000


class TaskStatus(StrEnum):
    """Canonical task status after synonym mapping."""

    NOT_STARTED = "not_started"
    WORKING_ON_IT = "working_on_it"
    BLOCKED = "blocked"
    DONE = "done"
    UNKNOWN = "unknown"


class BlockerCategory(StrEnum):
    """Closed set of reasons a task cannot move forward."""

    NONE = "none"
    MATERIALS = "materials"
    UPSTREAM_DEPENDENCY = "upstream_dependency"
    PERMIT = "permit"
    SITE_ACCESS = "site_access"
    OTHER = "other"
    UNCLEAR = "unclear"


class InterpretationSource(StrEnum):
    """Who produced a note interpretation."""

    KEYWORD = "keyword"
    LLM = "llm"
    FALLBACK = "fallback"


class DecisionKind(StrEnum):
    """The five decisions the brief allows."""

    NO_ACTION = "no_action"
    FOLLOW_UP_OWNER = "follow_up_owner"
    REQUEST_ETA = "request_eta"
    CLARIFY_MISSING_INFO = "clarify_missing_info"
    ESCALATE = "escalate"


class ReasonCode(StrEnum):
    """Machine-readable reason attached to every decision."""

    COMPLETED = "completed"
    MISSING_OWNER = "missing_owner"
    MISSING_DUE_DATE = "missing_due_date"
    MISSING_OWNER_AND_DUE_DATE = "missing_owner_and_due_date"
    UNKNOWN_STATUS = "unknown_status"
    MATERIALS_ETA_UNKNOWN = "materials_eta_unknown"
    BLOCKED_DEPENDENCY = "blocked_dependency"
    OVERDUE_ESCALATION = "overdue_escalation"
    OVERDUE_NO_EXPLANATION = "overdue_no_explanation"
    BLOCKED_REASON_UNCLEAR = "blocked_reason_unclear"
    DUE_SOON_NOT_STARTED = "due_soon_not_started"
    DUE_SOON_STALE = "due_soon_stale"
    ON_TRACK = "on_track"


class Priority(StrEnum):
    """Delivery priority of an outbound message."""

    NORMAL = "normal"
    HIGH = "high"


class TargetKind(StrEnum):
    """What kind of recipient a decision addresses."""

    PERSON = "person"
    ROLE = "role"
    TEAM = "team"


class Flag(StrEnum):
    """Audit flags; they explain a decision but never make one."""

    PROMPT_INJECTION_SUSPECTED = "prompt_injection_suspected"
    LLM_FALLBACK = "llm_fallback"
    NOTE_TRUNCATED = "note_truncated"
    INVALID_DUE_DATE = "invalid_due_date"
    INVALID_LAST_UPDATE = "invalid_last_update"
    FUTURE_LAST_UPDATE = "future_last_update"


@dataclass(frozen=True, slots=True)
class UntrustedText:
    """Free text from the task source, cleaned but never trusted.

    It is a distinct type, not a ``str``, so that every function that
    puts it into a prompt or a message has to accept it explicitly.
    """

    value: str
    truncated: bool = False

    @classmethod
    def of(cls, raw: object) -> Self:
        """Clean ``raw``; anything but a string becomes empty text."""
        if not isinstance(raw, str):
            return cls("")
        value, truncated = clean_block(raw, MAX_NOTE_CHARS)
        return cls(value, truncated)

    def __bool__(self) -> bool:
        """Return whether there is any text at all."""
        return bool(self.value)


@dataclass(frozen=True, slots=True)
class Task:
    """A normalised task; ``None`` means the source did not say."""

    task_id: TaskId
    project: str
    title: str
    owner: str | None
    due_date: date | None
    status: TaskStatus
    last_update: date | None
    notes: UntrustedText
    flags: frozenset[Flag] = frozenset()

    def days_until_due(self, as_of: date) -> int | None:
        """Return ``due_date - as_of`` in days (negative = overdue)."""
        if self.due_date is None:
            return None
        return (self.due_date - as_of).days

    def missing_fields(self) -> tuple[str, ...]:
        """Return the decision-relevant fields the source left empty."""
        missing = (
            ("owner", self.owner is None),
            ("due_date", self.due_date is None),
        )
        return tuple(name for name, absent in missing if absent)


@dataclass(frozen=True, slots=True)
class RejectedRecord:
    """A source record that could not become a task."""

    index: int
    task_id: str | None
    reason: str


@dataclass(frozen=True, slots=True)
class ScreenResult:
    """Outcome of the prompt-injection screen for one note."""

    suspicious: bool
    matched_patterns: tuple[str, ...] = ()


CLEAN_SCREEN = ScreenResult(suspicious=False)


@dataclass(frozen=True, slots=True)
class NoteInterpretation:
    """What a note says about blockers, with verifiable evidence.

    ``evidence`` is a verbatim quote from the cleaned note so that a
    reviewer (and the grounding check) can trace the classification.
    """

    blocker: BlockerCategory
    needs_eta: bool
    confidence: float
    evidence: str
    source: InterpretationSource


@dataclass(frozen=True, slots=True)
class Target:
    """Recipient of a decision; never taken from an LLM."""

    kind: TargetKind
    name: str
    project: str | None = None


@dataclass(frozen=True, slots=True)
class Decision:
    """Exactly one per task and run; produced only by the policy."""

    task_id: TaskId
    kind: DecisionKind
    target: Target | None
    rule_id: str
    reason_code: ReasonCode
    reason: str
    priority: Priority = Priority.NORMAL
    flags: frozenset[Flag] = field(default_factory=frozenset)
    interpretation: NoteInterpretation | None = None

    @property
    def actionable(self) -> bool:
        """Return whether the decision leads to an outbound message."""
        return self.kind is not DecisionKind.NO_ACTION
