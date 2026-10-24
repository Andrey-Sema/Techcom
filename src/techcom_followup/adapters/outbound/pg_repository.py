"""PostgreSQL outbox with unique inserts and fenced leases."""

from collections.abc import Mapping
from datetime import date, datetime
from uuid import UUID, uuid4

import sqlalchemy as sa
from pydantic import JsonValue, TypeAdapter
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from techcom_followup.adapters.outbound.orm import (
    action_keys,
    actions,
    audit_events,
    decisions,
    llm_cache,
    runs,
)
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
    DeliveryAuditKind,
    OutboxStatus,
    PruneCounts,
    RetentionCutoffs,
    RunRecord,
    StoredDecision,
    outcome_event,
    require_aware,
    review_event,
)
from techcom_followup.domain.messages import MessageDraft

_FOREIGN_KEY_VIOLATION = "23503"
_TERMINAL = ("sent", "dead")
_WRITE = TypeAdapter(DecisionWrite)
_DRAFT = TypeAdapter(MessageDraft)
_ACTION = TypeAdapter(ActionRecord)
_RUN = TypeAdapter(RunRecord)
_CACHE = TypeAdapter(CacheEntry)
_AUDIT = TypeAdapter(AuditEvent)
_ACTION_QUERY = sa.select(actions, decisions.c.data).join(
    decisions, actions.c.decision_id == decisions.c.id
)


def _stored(row: RowMapping) -> StoredDecision:
    return StoredDecision(
        row["id"],
        _WRITE.validate_python(row["data"]),
        DecisionOutcome(row["outcome"]),
    )


def _action(row: RowMapping) -> ActionRecord:
    data = dict(row)
    # Internal retry fence, enforced by the database transition trigger.
    data.pop("superseded", None)
    write = data.pop("data")
    data["identity"] = write["identity"]
    data["decision"] = write["decision"]
    data["message"] = write["message"]
    data["source_project"] = write.get("source_project")
    return _ACTION.validate_python(data)


def _ownership(
    action_id: UUID, token: UUID, now: datetime
) -> sa.ColumnElement[bool]:
    return sa.and_(
        actions.c.id == action_id,
        actions.c.lease_token == token,
        actions.c.lease_until > now,
        actions.c.status.in_(["in_flight", "unknown"]),
    )


class PgRepository:
    """Inject an engine; keep delivery I/O outside repository calls."""

    def __init__(self, engine: AsyncEngine) -> None:
        """Keep engine ownership with the composition root."""
        self._engine = engine

    async def start_run(
        self, as_of: date, policy_version: str, now: datetime
    ) -> RunRecord:
        """Let PostgreSQL 18 supply the UUIDv7 run identifier."""
        require_aware(now)
        async with self._engine.begin() as connection:
            result = await connection.execute(
                runs.insert()
                .values(
                    as_of=as_of, policy_version=policy_version, started_at=now
                )
                .returning(runs)
            )
            return _RUN.validate_python(dict(result.mappings().one()))

    async def finish_run(
        self, run_id: UUID, stats: Mapping[str, JsonValue], now: datetime
    ) -> bool:
        """Finish a run exactly once and detach its statistics."""
        require_aware(now)
        async with self._engine.begin() as connection:
            result = await connection.execute(
                runs.update()
                .where(runs.c.id == run_id, runs.c.finished_at.is_(None))
                .values(finished_at=now, stats=dict(stats))
                .returning(runs.c.id)
            )
            return result.scalar_one_or_none() is not None

    async def get_run(self, run_id: UUID) -> RunRecord | None:
        """Return a detached snapshot or absence."""
        async with self._engine.connect() as connection:
            result = await connection.execute(
                sa.select(runs).where(runs.c.id == run_id)
            )
            row = result.mappings().one_or_none()
            return None if row is None else _RUN.validate_python(dict(row))

    async def record_decision(
        self, write: DecisionWrite, now: datetime
    ) -> StoredDecision:
        """Commit decision, deduped action and audit together.

        One round trip per statement that changes state: the foreign key
        proves the run exists, and a fresh insert is returned from the
        write itself instead of being read back.
        """
        write.validate()
        require_aware(now)
        async with self._engine.begin() as connection:
            try:
                inserted = await connection.execute(
                    insert(decisions)
                    .values(
                        run_id=write.run_id,
                        task_id=write.decision.task_id,
                        data=_WRITE.dump_python(write, mode="json"),
                        state_fingerprint=write.fingerprint,
                        outcome="none",
                        source_project=write.source_project,
                        source_position=write.source_position,
                    )
                    .on_conflict_do_nothing(
                        index_elements=[
                            decisions.c.run_id,
                            decisions.c.task_id,
                        ]
                    )
                    .returning(decisions.c.id, decisions.c.outcome)
                )
            except sa.exc.IntegrityError as error:
                sqlstate = getattr(error.orig, "sqlstate", None)
                if sqlstate == _FOREIGN_KEY_VIOLATION:
                    message = "run does not exist"
                    raise ValueError(message) from error
                raise
            row = inserted.mappings().one_or_none()
            if row is None:
                # The same run already decided this task: replay it.
                result = await connection.execute(
                    sa.select(decisions).where(
                        decisions.c.run_id == write.run_id,
                        decisions.c.task_id == write.decision.task_id,
                    )
                )
                return _stored(result.mappings().one())
            decision_id = row["id"]
            outcome = await self._enqueue(
                connection,
                write,
                decision_id,
                now,
                DecisionOutcome(row["outcome"]),
            )
            return StoredDecision(decision_id, write, outcome)

    async def _enqueue(
        self,
        connection: AsyncConnection,
        write: DecisionWrite,
        decision_id: UUID,
        now: datetime,
        outcome: DecisionOutcome,
    ) -> DecisionOutcome:
        identity = write.identity
        if (
            identity is not None
            and outcome is not DecisionOutcome.STALE_SUPPRESSED
        ):
            result = await connection.execute(
                insert(actions)
                .values(
                    idempotency_key=identity.idempotency_key,
                    operation_id=identity.operation_id,
                    decision_id=decision_id,
                    status=write.initial_status.value,
                    next_attempt_at=now,
                )
                .on_conflict_do_nothing()
                .returning(actions.c.id)
            )
            outcome = (
                DecisionOutcome.ENQUEUED
                if result.scalar_one_or_none() is not None
                else DecisionOutcome.DUPLICATE_SUPPRESSED
            )
        statement = audit_events.insert().values(
            kind=f"decision.{outcome.value}",
            at=now,
            run_id=write.run_id,
            task_id=write.decision.task_id,
            payload=write.audit_payload(decision_id),
        )
        if identity is not None:
            # PostgreSQL executes a data-modifying CTE exactly once even
            # without a reference. The existing decision row is visible
            # from the preceding statement in this same transaction.
            statement = statement.add_cte(
                decisions.update()
                .where(decisions.c.id == decision_id)
                .values(outcome=outcome.value)
                .returning(decisions.c.id)
                .cte("recorded_outcome")
            )
        await connection.execute(statement)
        return outcome

    async def list_decisions(self, run_id: UUID) -> tuple[StoredDecision, ...]:
        """Return stable insertion order using UUIDv7 row keys."""
        async with self._engine.connect() as connection:
            result = await connection.execute(
                sa.select(decisions)
                .where(decisions.c.run_id == run_id)
                .order_by(
                    decisions.c.source_position.asc().nulls_last(),
                    decisions.c.id,
                )
            )
            return tuple(_stored(row) for row in result.mappings())

    async def list_actions(self) -> tuple[ActionRecord, ...]:
        """Return detached outbox records for reporting."""
        async with self._engine.connect() as connection:
            result = await connection.execute(
                _ACTION_QUERY.order_by(actions.c.id)
            )
            return tuple(_action(row) for row in result.mappings())

    async def list_run_actions(self, run_id: UUID) -> tuple[ActionRecord, ...]:
        """Join indexed run/decision keys, not the whole outbox."""
        statement = _ACTION_QUERY.where(decisions.c.run_id == run_id).order_by(
            decisions.c.source_position.asc().nulls_last(), actions.c.id
        )
        async with self._engine.connect() as connection:
            result = await connection.execute(statement)
            return tuple(_action(row) for row in result.mappings())

    async def get_action_messages(
        self, keys: tuple[str, ...]
    ) -> Mapping[str, MessageDraft | None]:
        """Look up requested identities; tombstones have no payload."""
        if len(keys) > MAX_BATCH_SIZE:
            message = "message lookup exceeds the batch bound"
            raise ValueError(message)
        if not keys:
            return {}
        statement = (
            sa.select(
                action_keys.c.idempotency_key,
                decisions.c.data["message"].label("message"),
            )
            .select_from(
                action_keys.outerjoin(
                    actions,
                    action_keys.c.idempotency_key == actions.c.idempotency_key,
                ).outerjoin(decisions, actions.c.decision_id == decisions.c.id)
            )
            .where(action_keys.c.idempotency_key.in_(keys))
        )
        async with self._engine.connect() as connection:
            result = await connection.execute(statement)
            return {
                row["idempotency_key"]: None
                if row["message"] is None
                else _DRAFT.validate_python(row["message"])
                for row in result.mappings()
            }

    async def get_action_plans(
        self, keys: tuple[str, ...]
    ) -> Mapping[str, DecisionWrite | None]:
        """Read complete plans or payload-free tombstones."""
        if len(keys) > MAX_BATCH_SIZE:
            message = "plan lookup exceeds the batch bound"
            raise ValueError(message)
        if not keys:
            return {}
        statement = (
            sa.select(action_keys.c.idempotency_key, decisions.c.data)
            .select_from(
                action_keys.outerjoin(
                    actions,
                    action_keys.c.idempotency_key == actions.c.idempotency_key,
                ).outerjoin(decisions, actions.c.decision_id == decisions.c.id)
            )
            .where(action_keys.c.idempotency_key.in_(keys))
        )
        async with self._engine.connect() as connection:
            rows = await connection.execute(statement)
            return {
                row["idempotency_key"]: None
                if row["data"] is None
                else _WRITE.validate_python(row["data"])
                for row in rows.mappings()
            }

    async def query_actions(
        self, query: ActionReadQuery
    ) -> tuple[ActionRecord, ...]:
        """Filter project authority in SQL before materializing rows."""
        query.validate()
        target = decisions.c.data["decision"]["target"]
        project = target["project"].as_string()
        statement = _ACTION_QUERY.where(target.astext.is_not(None))
        if not query.all_projects:
            statement = statement.where(
                decisions.c.source_project.in_(sorted(query.projects)),
                project.in_(sorted(query.projects)),
            )
        if query.project is not None:
            statement = statement.where(
                decisions.c.source_project == query.project,
                project == query.project,
            )
        if query.task_id is not None:
            statement = statement.where(decisions.c.task_id == query.task_id)
        if query.status is not None:
            statement = statement.where(actions.c.status == query.status.value)
        order = actions.c.id.desc() if query.newest_first else actions.c.id
        async with self._engine.connect() as connection:
            result = await connection.execute(
                statement.order_by(order).limit(query.limit)
            )
            return tuple(_action(row) for row in result.mappings())

    async def review_held(
        self, operation_id: UUID, *, approve: bool, now: datetime
    ) -> ActionRecord | None:
        """Leave review in one transaction with its audit event."""
        require_aware(now)
        status = ActionStatus.PENDING if approve else ActionStatus.DEAD
        async with self._engine.begin() as connection:
            updated = await connection.execute(
                actions.update()
                .where(
                    actions.c.operation_id == operation_id,
                    actions.c.status == ActionStatus.HELD.value,
                )
                .values(
                    status=status.value,
                    next_attempt_at=now,
                    last_error=None if approve else REVIEW_REJECTED,
                    terminal_at=None if approve else now,
                )
                .returning(actions.c.id)
            )
            action_id = updated.scalar_one_or_none()
            if action_id is None:
                return None
            row = await connection.execute(
                _ACTION_QUERY.where(actions.c.id == action_id)
            )
            reviewed = _action(row.mappings().one())
            event = review_event(reviewed, approve=approve, now=now)
            await connection.execute(
                audit_events.insert().values(
                    kind=event.kind,
                    at=event.at,
                    payload=dict(event.payload),
                    task_id=event.task_id,
                )
            )
            return reviewed

    async def outbox_summary(self) -> tuple[OutboxStatus, ...]:
        """Group actions by status with their earliest due time."""
        statement = (
            sa.select(
                actions.c.status,
                sa.func.count(),
                sa.func.min(actions.c.next_attempt_at),
            )
            .group_by(actions.c.status)
            .order_by(actions.c.status)
        )
        async with self._engine.connect() as connection:
            rows = (await connection.execute(statement)).all()
        return tuple(
            OutboxStatus(ActionStatus(status), count, earliest)
            for status, count, earliest in rows
        )

    async def claim_actions(
        self, request: ClaimRequest
    ) -> tuple[ActionRecord, ...]:
        """Claim pending sends; recovery requires reconciliation."""
        return await self._claim(request, ActionStatus.PENDING)

    async def claim_unknown(
        self, request: ClaimRequest
    ) -> tuple[ActionRecord, ...]:
        """Claim unknown work without counting a delivery attempt."""
        return await self._claim(request, ActionStatus.UNKNOWN)

    async def _claim(
        self, request: ClaimRequest, status: ActionStatus
    ) -> tuple[ActionRecord, ...]:
        request.validate()
        async with self._engine.begin() as connection:
            await connection.execute(
                actions.update()
                .where(
                    actions.c.status == "in_flight",
                    actions.c.lease_until <= request.now,
                )
                .values(
                    status="unknown",
                    lease_token=None,
                    lease_until=None,
                    next_attempt_at=request.now,
                    last_error="lease_expired",
                )
            )
            selected = await connection.execute(
                sa.select(actions.c.id)
                .where(
                    actions.c.status == status.value,
                    actions.c.next_attempt_at <= request.now,
                    sa.or_(
                        actions.c.lease_until.is_(None),
                        actions.c.lease_until <= request.now,
                    ),
                )
                .order_by(actions.c.next_attempt_at, actions.c.id)
                .limit(request.limit)
                .with_for_update(skip_locked=True)
            )
            ids = tuple(selected.scalars())
            if not ids:
                return ()
            await connection.execute(
                actions.update()
                .where(actions.c.id.in_(ids))
                .values(
                    status="in_flight"
                    if status == ActionStatus.PENDING
                    else "unknown",
                    attempts=actions.c.attempts
                    + int(status == ActionStatus.PENDING),
                    lease_token=uuid4(),
                    lease_until=request.now + request.lease,
                )
            )
            result = await connection.execute(
                _ACTION_QUERY.where(actions.c.id.in_(ids)).order_by(
                    actions.c.id
                )
            )
            return tuple(_action(row) for row in result.mappings())

    async def apply_outcome(
        self,
        update: ActionUpdate,
        *,
        audit_kind: DeliveryAuditKind | None = None,
    ) -> ActionStatus | None:
        """Fence stale workers from modifying newer claims."""
        update.validate()
        async with self._engine.begin() as connection:
            result = await connection.execute(
                actions.update()
                .where(
                    _ownership(
                        update.action_id, update.lease_token, update.now
                    )
                )
                .values(
                    status=update.status.value,
                    next_attempt_at=update.next_attempt_at,
                    last_error=update.error,
                    provider_ref=update.provider_ref,
                    lease_token=None,
                    lease_until=None,
                    sent_at=update.now
                    if update.status == ActionStatus.SENT
                    else None,
                    terminal_at=update.now
                    if update.status.value in _TERMINAL
                    else None,
                )
                .returning(actions.c.id, actions.c.status)
            )
            row = result.mappings().one_or_none()
            if row is None:
                return None
            status = ActionStatus(row["status"])
            if audit_kind is not None:
                saved = await connection.execute(
                    _ACTION_QUERY.where(actions.c.id == row["id"])
                )
                event = outcome_event(
                    _action(saved.mappings().one()),
                    update.now,
                    status,
                    audit_kind,
                )
                await connection.execute(
                    audit_events.insert().values(
                        kind=event.kind,
                        at=event.at,
                        payload=dict(event.payload),
                        task_id=event.task_id,
                    )
                )
            return status

    async def defer_claim(self, update: ActionUpdate) -> bool:
        """Fence an unsent claim and restore its unspent attempt."""
        update.validate_defer()
        async with self._engine.begin() as connection:
            result = await connection.execute(
                actions.update()
                .where(
                    _ownership(
                        update.action_id, update.lease_token, update.now
                    ),
                    actions.c.status == ActionStatus.IN_FLIGHT.value,
                    actions.c.attempts >= 1,
                )
                .values(
                    status=ActionStatus.PENDING.value,
                    attempts=actions.c.attempts - 1,
                    next_attempt_at=update.next_attempt_at,
                    last_error=update.error,
                    lease_token=None,
                    lease_until=None,
                )
                .returning(actions.c.id)
            )
            return result.scalar_one_or_none() is not None

    async def release_claim(
        self, action_id: UUID, lease_token: UUID, now: datetime
    ) -> bool:
        """Release ambiguous work for lookup before replay."""
        return (
            await self.apply_outcome(
                ActionUpdate(
                    action_id, lease_token, now, ActionStatus.UNKNOWN, now
                )
            )
            is not None
        )

    async def put_cached_llm(self, entry: CacheEntry) -> None:
        """Upsert validated classifications with a finite lifetime."""
        entry.validate()
        statement = insert(llm_cache).values(
            cache_key=entry.key,
            data=_CACHE.dump_python(entry, mode="json"),
            expires_at=entry.expires_at,
        )
        async with self._engine.begin() as connection:
            await connection.execute(
                statement.on_conflict_do_update(
                    index_elements=[llm_cache.c.cache_key],
                    set_={
                        "data": statement.excluded.data,
                        "expires_at": statement.excluded.expires_at,
                    },
                )
            )

    async def get_cached_llm(
        self, key: str, now: datetime
    ) -> CacheEntry | None:
        """Read an entry only strictly before its expiry instant."""
        require_aware(now)
        async with self._engine.connect() as connection:
            data = await connection.scalar(
                sa.select(llm_cache.c.data).where(
                    llm_cache.c.cache_key == key, llm_cache.c.expires_at > now
                )
            )
            return None if data is None else _CACHE.validate_python(data)

    async def append_audit(self, event: AuditEvent) -> None:
        """Append metadata in its own short transaction."""
        require_aware(event.at)
        async with self._engine.begin() as connection:
            await connection.execute(
                audit_events.insert().values(
                    kind=event.kind,
                    at=event.at,
                    payload=dict(event.payload),
                    run_id=event.run_id,
                    task_id=event.task_id,
                )
            )

    async def list_audit(self) -> tuple[AuditEvent, ...]:
        """Read the append-only trail in insertion order."""
        async with self._engine.connect() as connection:
            result = await connection.execute(
                sa.select(audit_events).order_by(audit_events.c.id)
            )
            return tuple(
                _AUDIT.validate_python(
                    {
                        "kind": row["kind"],
                        "at": row["at"],
                        "payload": row["payload"],
                        "run_id": row["run_id"],
                        "task_id": row["task_id"],
                    }
                )
                for row in result.mappings()
            )

    async def prune(
        self, cutoffs: RetentionCutoffs, limit: int
    ) -> PruneCounts:
        """Delete one bounded batch per table in a single transaction.

        Order matters: actions go first, so their decisions become
        eligible in the same batch, and runs go last.
        """
        unreferenced = ~sa.exists().where(
            actions.c.decision_id == decisions.c.id
        )
        old_runs = sa.select(runs.c.id).where(
            runs.c.finished_at < cutoffs.decisions
        )
        steps = (
            (
                llm_cache,
                llm_cache.c.cache_key,
                llm_cache.c.expires_at <= cutoffs.cache,
            ),
            (
                actions,
                actions.c.id,
                actions.c.status.in_(_TERMINAL)
                & (actions.c.terminal_at < cutoffs.actions),
            ),
            (
                decisions,
                decisions.c.id,
                decisions.c.run_id.in_(old_runs) & unreferenced,
            ),
            (
                audit_events,
                audit_events.c.id,
                audit_events.c.at < cutoffs.audit,
            ),
            (
                runs,
                runs.c.id,
                (runs.c.finished_at < cutoffs.decisions)
                & ~sa.exists().where(decisions.c.run_id == runs.c.id),
            ),
        )
        deleted: list[int] = []
        async with self._engine.begin() as connection:
            for table, key, condition in steps:
                victims = sa.select(key).where(condition).limit(limit)
                result = await connection.execute(
                    sa.delete(table).where(key.in_(victims))
                )
                deleted.append(result.rowcount)
            history = await connection.execute(
                sa.text(
                    "SELECT public.prune_admission_history(:before, :bound)"
                ),
                {"before": cutoffs.actions, "bound": limit},
            )
            admission_rows = int(history.scalar_one())
        cache, action_rows, decision_rows, audit, run_rows = deleted
        return PruneCounts(
            cache,
            action_rows,
            decision_rows,
            audit,
            run_rows,
            admissions=admission_rows,
        )
