"""Workflow contracts independent of storage and transport libraries."""

from collections.abc import Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from pydantic import JsonValue

from techcom_followup.application.repository import ActionRecord
from techcom_followup.domain.messages import MessageDraft
from techcom_followup.domain.models import (
    Decision,
    NoteInterpretation,
    ScreenResult,
    Task,
)


class Clock(Protocol):
    """Aware UTC time supplied by the composition root."""

    def now(self) -> datetime:
        """Return the current instant."""
        ...

    def today(self) -> date:
        """Return the UTC calendar date."""
        ...


class TaskSource(Protocol):
    """Bounded source records, normalized by the application."""

    async def fetch_tasks(self) -> Sequence[object]:
        """Fetch records or raise a closed source error."""
        ...


class NoteInterpreter(Protocol):
    """Interpret notes without deciding recipients or actions."""

    async def interpret(
        self, task: Task, *, as_of: date, screen: ScreenResult
    ) -> NoteInterpretation | None:
        """Return validated evidence or a deterministic fallback."""
        ...


class MessageDrafter(Protocol):
    """Draft wording around code-owned decision facts."""

    async def draft(
        self, decision: Decision, task: Task, *, as_of: date
    ) -> MessageDraft:
        """Return a validated rendered message."""
        ...


@dataclass(frozen=True, slots=True)
class PlannedAction:
    """A reviewed action plan and its closed, text-free audit record."""

    decision: Decision
    message: MessageDraft
    audit: Mapping[str, JsonValue]


class ActionPlanner(Protocol):
    """Choose and phrase an action inside the policy's envelope."""

    async def plan(
        self, decision: Decision, task: Task, *, as_of: date
    ) -> PlannedAction:
        """Return a guarded plan, or the baseline with its template."""
        ...


class RunLock(Protocol):
    """Cross-worker exclusion for the whole processing lifecycle."""

    def acquire(self) -> AbstractAsyncContextManager[bool]:
        """Hold ownership until context exit, or report contention."""
        ...


class DeliveryKind(StrEnum):
    """Closed transport outcomes, without sensitive provider bodies."""

    ACCEPTED = "accepted"
    RETRYABLE = "retryable"
    PERMANENT = "permanent"
    UNKNOWN = "unknown"
    ABSENT = "absent"


@dataclass(frozen=True, slots=True)
class DeliveryResult:
    """One send or lookup result; absent is lookup-only."""

    kind: DeliveryKind
    provider_ref: str | None = None
    error: str | None = None
    retry_after: float = 0


class DeliveryGateway(Protocol):
    """Exactly one request per method; retries belong to use cases."""

    async def send(self, action: ActionRecord) -> DeliveryResult:
        """Attempt a write once with its stable operation identity."""
        ...

    async def lookup(self, operation_id: UUID) -> DeliveryResult:
        """Distinguish acknowledged absence from uncertain lookup."""
        ...
