"""Process source records through policy and an atomic outbox write."""

from asyncio import CancelledError
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from uuid import UUID

from pydantic import JsonValue

from techcom_followup.application.processing_ports import (
    Clock,
    MessageDrafter,
    NoteInterpreter,
    RunLock,
    TaskSource,
)
from techcom_followup.application.repository import (
    AuditEvent,
    DecisionOutcome,
    DecisionWrite,
    Repository,
)
from techcom_followup.domain.identity import ActionIdentity, state_fingerprint
from techcom_followup.domain.injection import screen
from techcom_followup.domain.interpretation import prefilter
from techcom_followup.domain.messages import MessageDraft, compose
from techcom_followup.domain.models import Decision, RejectedRecord, Task
from techcom_followup.domain.normalize import normalize
from techcom_followup.domain.policy import DecisionPolicy


@dataclass(frozen=True, slots=True)
class RunReport:
    """Detached decisions and counters suitable for CLI/API."""

    run_id: UUID | None
    status: str
    decisions: tuple[Decision, ...] = ()
    rejected: tuple[RejectedRecord, ...] = ()
    enqueued: int = 0
    duplicates: int = 0


@dataclass(frozen=True, slots=True)
class ProcessingDependencies:
    """Required collaborators, grouped to keep construction explicit."""

    repository: Repository
    source: TaskSource
    policy: DecisionPolicy
    clock: Clock
    run_lock: RunLock


@dataclass(slots=True)
class _Progress:
    decisions: list[Decision] = field(default_factory=list)
    rejected: tuple[RejectedRecord, ...] = ()
    enqueued: int = 0
    duplicates: int = 0

    def stats(self, status: str) -> dict[str, JsonValue]:
        return {
            "status": status,
            "decisions": len(self.decisions),
            "rejected": len(self.rejected),
            "enqueued": self.enqueued,
            "duplicates": self.duplicates,
        }


class ProcessTasks:
    """A run owns its advisory lock before fetching or writing."""

    def __init__(
        self,
        dependencies: ProcessingDependencies,
        *,
        interpreter: NoteInterpreter | None = None,
        drafter: MessageDrafter | None = None,
    ) -> None:
        """Inject policy, I/O ports and optional validated LLM."""
        self._deps = dependencies
        self._interpreter = interpreter
        self._drafter = drafter

    async def execute(
        self, *, as_of: date, dry_run: bool = False
    ) -> RunReport:
        """Compute decisions; dry runs perform zero persistence."""
        async with self._deps.run_lock.acquire() as acquired:
            if not acquired:
                return RunReport(None, "skipped_locked")
            return await self._run(as_of, dry_run=dry_run)

    async def _run(self, as_of: date, *, dry_run: bool) -> RunReport:
        deps = self._deps
        run = (
            None
            if dry_run
            else await deps.repository.start_run(
                as_of, deps.policy.version, deps.clock.now()
            )
        )
        progress = _Progress()
        status = "completed"
        try:
            result = normalize(await deps.source.fetch_tasks(), as_of)
            existing = {
                row.identity.idempotency_key: row.message
                for row in await deps.repository.list_actions()
            }
            progress.rejected = result.rejected
            if run is not None:
                await self._rejects(run.id, result.rejected)
            for task in result.tasks:
                write = await self._prepare(
                    task, as_of, existing=existing, dry_run=dry_run
                )
                progress.decisions.append(write.decision)
                if run is not None:
                    await self._persist(run.id, write, progress)
        except (Exception, CancelledError):
            status = "failed"
            raise
        finally:
            if run is not None:
                await deps.repository.finish_run(
                    run.id, progress.stats(status), deps.clock.now()
                )
        return RunReport(
            None if run is None else run.id,
            "dry_run" if dry_run else status,
            tuple(progress.decisions),
            progress.rejected,
            progress.enqueued,
            progress.duplicates,
        )

    async def _prepare(
        self,
        task: Task,
        as_of: date,
        *,
        existing: Mapping[str, MessageDraft],
        dry_run: bool,
    ) -> DecisionWrite:
        screening = screen(task.notes)
        interpretation = (
            None if screening.suspicious else prefilter(task.notes)
        )
        if self._interpreter is not None and not dry_run:
            interpretation = await self._interpreter.interpret(
                task, as_of=as_of, screen=screening
            )
        decision = self._deps.policy.decide(
            task, as_of=as_of, screen=screening, interpretation=interpretation
        )
        fingerprint = state_fingerprint(task)
        identity = None
        draft = None
        if decision.actionable:
            identity = ActionIdentity.of(decision, fingerprint)
            draft = existing.get(identity.idempotency_key)
            if draft is None:
                draft = await self._draft(
                    decision, task, as_of, dry_run=dry_run
                )
        return DecisionWrite(
            UUID(int=0), decision, fingerprint, identity, draft
        )

    async def _draft(
        self, decision: Decision, task: Task, as_of: date, *, dry_run: bool
    ) -> MessageDraft:
        if self._drafter is None or dry_run:
            return compose(decision, task, as_of)
        return await self._drafter.draft(decision, task, as_of=as_of)

    async def _persist(
        self, run_id: UUID, write: DecisionWrite, progress: _Progress
    ) -> None:
        stored = await self._deps.repository.record_decision(
            DecisionWrite(
                run_id,
                write.decision,
                write.fingerprint,
                write.identity,
                write.message,
            ),
            self._deps.clock.now(),
        )
        progress.enqueued += stored.outcome is DecisionOutcome.ENQUEUED
        progress.duplicates += (
            stored.outcome is DecisionOutcome.DUPLICATE_SUPPRESSED
        )

    async def _rejects(
        self, run_id: UUID, rejected: tuple[RejectedRecord, ...]
    ) -> None:
        for record in rejected:
            await self._deps.repository.append_audit(
                AuditEvent(
                    "task.rejected",
                    self._deps.clock.now(),
                    {"index": record.index, "reason": record.reason},
                    run_id,
                    record.task_id,
                )
            )
