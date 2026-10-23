"""Process source records through policy and an atomic outbox write."""

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
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
from techcom_followup.domain.normalize import UNKNOWN_LABEL, normalize
from techcom_followup.domain.policy import DecisionPolicy

DEFAULT_WRITE_CONCURRENCY = 8
MAX_WRITE_CONCURRENCY = 16


@dataclass(frozen=True, slots=True)
class RunReport:
    """Detached decisions and counters suitable for CLI/API."""

    run_id: UUID | None
    status: str
    decisions: tuple[Decision, ...] = ()
    rejected: tuple[RejectedRecord, ...] = ()
    enqueued: int = 0
    duplicates: int = 0
    plans: tuple[DecisionWrite, ...] = ()


@dataclass(frozen=True, slots=True)
class ProcessingDependencies:
    """Required collaborators, grouped to keep construction explicit."""

    repository: Repository
    source: TaskSource
    policy: DecisionPolicy
    clock: Clock
    run_lock: RunLock


@dataclass(frozen=True, slots=True)
class _Proposal:
    task: Task
    decision: Decision
    fingerprint: str
    identity: ActionIdentity | None


@dataclass(slots=True)
class _Progress:
    run_id: UUID | None = None
    decisions: list[Decision] = field(default_factory=list)
    plans: list[DecisionWrite] = field(default_factory=list)
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
        write_concurrency: int = DEFAULT_WRITE_CONCURRENCY,
    ) -> None:
        """Inject policy, I/O ports and optional LLM workflows."""
        self._deps = dependencies
        self._interpreter = interpreter
        self._drafter = drafter
        if (
            type(write_concurrency) is not int
            or not 1 <= write_concurrency <= MAX_WRITE_CONCURRENCY
        ):
            message = "write concurrency must be in 1..16"
            raise ValueError(message)
        self._write_concurrency = write_concurrency

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
        progress = _Progress(run_id=None if run is None else run.id)
        status = "completed"
        try:
            result = normalize(await deps.source.fetch_tasks(), as_of)
            progress.rejected = result.rejected
            if run is not None:
                await self._rejects(run.id, result.rejected)
            await self._process(result.tasks, progress, as_of, dry_run=dry_run)
        except (Exception, asyncio.CancelledError):
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
            plans=tuple(progress.plans),
        )

    async def _process(
        self,
        tasks: tuple[Task, ...],
        progress: _Progress,
        as_of: date,
        *,
        dry_run: bool,
    ) -> None:
        width = self._write_concurrency
        for start in range(0, len(tasks), width):
            proposals = [
                await self._proposal(task, as_of, dry_run=dry_run)
                for task in tasks[start : start + width]
            ]
            keys = tuple(
                item.identity.idempotency_key
                for item in proposals
                if item.identity is not None
            )
            existing = (
                {}
                if dry_run
                else await self._deps.repository.get_action_messages(keys)
            )
            batch: list[DecisionWrite] = []
            for position, proposal in enumerate(proposals, start):
                write = await self._prepare(
                    proposal, as_of, existing=existing, dry_run=dry_run
                )
                write = replace(write, source_position=position)
                progress.decisions.append(write.decision)
                progress.plans.append(write)
                batch.append(write)
            if progress.run_id is not None:
                await self._persist_batch(progress.run_id, batch, progress)

    async def _proposal(
        self,
        task: Task,
        as_of: date,
        *,
        dry_run: bool,
    ) -> _Proposal:
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
        identity = (
            ActionIdentity.of(decision, fingerprint)
            if decision.actionable
            else None
        )
        return _Proposal(task, decision, fingerprint, identity)

    async def _prepare(
        self,
        proposal: _Proposal,
        as_of: date,
        *,
        existing: Mapping[str, MessageDraft | None],
        dry_run: bool,
    ) -> DecisionWrite:
        identity, task, decision = (
            proposal.identity,
            proposal.task,
            proposal.decision,
        )
        draft = None
        if identity is not None:
            if identity.idempotency_key in existing:
                # Retained identities need no new model call, even after
                # the original payload has expired. No action is sent.
                draft = existing[identity.idempotency_key] or compose(
                    decision, task, as_of
                )
            else:
                draft = await self._draft(
                    decision, task, as_of, dry_run=dry_run
                )
        return DecisionWrite(
            UUID(int=0),
            decision,
            proposal.fingerprint,
            identity,
            draft,
            source_project=(
                None if task.project == UNKNOWN_LABEL else task.project
            ),
        )

    async def _persist_batch(
        self, run_id: UUID, writes: list[DecisionWrite], progress: _Progress
    ) -> None:
        """Join or cancel every writer before finalizing the run.

        Preparation stays ordered and sequential: language admission and
        caches retain their existing semantics. Only atomic persistence
        overlaps, with at most sixteen transactions and no task per
        input.
        """
        try:
            async with asyncio.TaskGroup() as group:
                pending = [
                    group.create_task(self._persist(run_id, write, progress))
                    for write in writes
                ]
            # TaskGroup treats a cancelled child as normal completion;
            # a run must instead fail if any receipt was never obtained.
            for task in pending:
                task.result()
        except ExceptionGroup as errors:
            if len(errors.exceptions) == 1:
                raise errors.exceptions[0] from None
            raise

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
            replace(write, run_id=run_id),
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
