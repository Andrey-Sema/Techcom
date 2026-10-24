"""Minimal structural ports for API composition and event export."""

from dataclasses import dataclass
from datetime import date, datetime
from typing import Protocol
from uuid import UUID


class RunResult(Protocol):
    """The public subset of the processing use case's run report."""

    @property
    def run_id(self) -> UUID | None:
        """Persisted identifier, absent for dry runs or skips."""
        ...

    @property
    def status(self) -> str:
        """Completion, dry-run or lock-skip outcome."""
        ...

    @property
    def enqueued(self) -> int:
        """New actions persisted by this run."""
        ...

    @property
    def duplicates(self) -> int:
        """Existing actions suppressed by deduplication."""
        ...

    @property
    def stale(self) -> int:
        """Snapshots fenced as older than the source watermark."""
        ...


class RunService(Protocol):
    """Execute global processing behind a structural port."""

    async def execute(
        self, *, as_of: date, dry_run: bool = False
    ) -> RunResult:
        """Run processing; authorization must precede this call."""
        ...


class Clock(Protocol):
    """Inject both UTC and monotonic clocks for security decisions."""

    def now(self) -> datetime:
        """Return an aware UTC timestamp."""
        ...

    def monotonic(self) -> float:
        """Return elapsed seconds for process-local rate limits."""
        ...


@dataclass(frozen=True, slots=True)
class SecurityEvent:
    """Controlled metadata without tokens, notes or names."""

    name: str
    request_id: UUID
    status: int
    actor_id: str | None = None


class EventSink(Protocol):
    """Export metadata to structured logs or OTLP."""

    def emit(self, event: SecurityEvent) -> None:
        """Enqueue an event without blocking request processing."""
        ...


class RequestLimiter(Protocol):
    """Admission shared by every API instance in a deployment."""

    @property
    def retry_after(self) -> str:
        """A conservative retry interval in seconds."""
        ...

    async def allow(self, namespace: str, actor_id: str) -> bool:
        """Atomically reserve a bounded request or failed-auth slot."""
        ...
