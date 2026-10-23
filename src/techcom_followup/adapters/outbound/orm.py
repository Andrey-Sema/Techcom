"""SQLAlchemy Core schema with PostgreSQL 18 UUIDv7 row identifiers."""

import uuid

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

metadata = sa.MetaData()


def _id() -> sa.Column[uuid.UUID]:
    return sa.Column(
        "id",
        UUID(as_uuid=True),
        primary_key=True,
        server_default=sa.text("uuidv7()"),
    )


runs = sa.Table(
    "runs",
    metadata,
    _id(),
    sa.Column("as_of", sa.Date, nullable=False),
    sa.Column("policy_version", sa.Text, nullable=False),
    sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("finished_at", sa.DateTime(timezone=True)),
    sa.Column("stats", JSONB, nullable=False, server_default="{}"),
)
decisions = sa.Table(
    "decisions",
    metadata,
    _id(),
    sa.Column("run_id", UUID, sa.ForeignKey("runs.id"), nullable=False),
    sa.Column("task_id", sa.Text, nullable=False),
    sa.Column("data", JSONB, nullable=False),
    sa.Column("state_fingerprint", sa.Text, nullable=False),
    sa.Column("outcome", sa.Text, nullable=False),
    sa.Column("source_project", sa.Text),
    sa.Column("source_position", sa.Integer),
    sa.UniqueConstraint("run_id", "task_id"),
    sa.CheckConstraint(
        "outcome IN ('none','enqueued','duplicate_suppressed')",
        name="decisions_outcome_check",
    ),
    sa.CheckConstraint(
        "source_position >= 0", name="decisions_source_position_check"
    ),
)
sa.Index("decisions_project_idx", decisions.c.source_project, decisions.c.id)
sa.Index("decisions_task_idx", decisions.c.task_id, decisions.c.id)
sa.Index(
    "decisions_run_position_idx",
    decisions.c.run_id,
    decisions.c.source_position,
    decisions.c.id,
)
action_status = sa.Enum(
    "pending", "in_flight", "unknown", "sent", "dead", name="action_status"
)
actions = sa.Table(
    "actions",
    metadata,
    _id(),
    sa.Column("idempotency_key", sa.Text, unique=True, nullable=False),
    sa.Column("operation_id", UUID, unique=True, nullable=False),
    sa.Column(
        "decision_id", UUID, sa.ForeignKey("decisions.id"), nullable=False
    ),
    sa.Column(
        "status", action_status, nullable=False, server_default="pending"
    ),
    sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
    sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("lease_until", sa.DateTime(timezone=True)),
    sa.Column("lease_token", UUID),
    sa.Column("last_error", sa.String(200)),
    sa.Column("provider_ref", sa.Text),
    sa.Column("sent_at", sa.DateTime(timezone=True)),
    sa.Column("terminal_at", sa.DateTime(timezone=True)),
    sa.CheckConstraint("attempts >= 0", name="actions_attempts_check"),
    sa.CheckConstraint(
        "(lease_until IS NULL) = (lease_token IS NULL)",
        name="actions_lease_check",
    ),
    sa.CheckConstraint(
        "(status IN ('sent', 'dead')) = (terminal_at IS NOT NULL)",
        name="actions_terminal_check",
    ),
)
sa.Index(
    "actions_terminal_idx",
    actions.c.terminal_at,
    postgresql_where=actions.c.status.in_(["sent", "dead"]),
)
sa.Index("actions_decision_idx", actions.c.decision_id, actions.c.id)
sa.Index(
    "actions_due_idx",
    actions.c.status,
    actions.c.next_attempt_at,
    postgresql_where=actions.c.status.in_(["pending", "unknown"]),
)
sa.Index(
    "actions_expired_idx",
    actions.c.lease_until,
    postgresql_where=actions.c.status == "in_flight",
)
llm_cache = sa.Table(
    "llm_cache",
    metadata,
    sa.Column("cache_key", sa.Text, primary_key=True),
    sa.Column("data", JSONB, nullable=False),
    sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
)
audit_events = sa.Table(
    "audit_events",
    metadata,
    _id(),
    sa.Column("kind", sa.Text, nullable=False),
    sa.Column("at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("payload", JSONB, nullable=False),
    sa.Column("run_id", UUID),
    sa.Column("task_id", sa.Text),
)

action_keys = sa.Table(
    "action_keys",
    metadata,
    sa.Column("idempotency_key", sa.Text, primary_key=True),
    sa.Column("operation_id", UUID, unique=True, nullable=False),
)
# Written only by the actions trigger (migration 0006); never pruned.
notification_slots = sa.Table(
    "notification_slots",
    metadata,
    sa.Column("task_id", sa.Text, primary_key=True),
    sa.Column("project", sa.Text, primary_key=True),
    sa.Column("state_fingerprint", sa.Text, primary_key=True),
    sa.Column("stage", sa.Text, primary_key=True),
    sa.CheckConstraint("stage IN ('notify', 'escalate')"),
)
api_rate_buckets = sa.Table(
    "api_rate_buckets",
    metadata,
    sa.Column("namespace", sa.Text, primary_key=True),
    sa.Column("actor_hash", sa.Text, primary_key=True),
    sa.Column("reset_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("used", sa.Integer, nullable=False),
    sa.CheckConstraint("namespace IN ('requests', 'failures')"),
    sa.CheckConstraint("actor_hash ~ '^[a-f0-9]{64}$'"),
    sa.CheckConstraint("used > 0"),
)
sa.Index(
    "api_rate_expiry_idx",
    api_rate_buckets.c.namespace,
    api_rate_buckets.c.reset_at,
)
recipient_days = sa.Table(
    "recipient_days",
    metadata,
    sa.Column("recipient_hash", sa.Text, primary_key=True),
    sa.Column("day", sa.Date, primary_key=True),
    sa.Column("used", sa.Integer, nullable=False),
    sa.CheckConstraint("used > 0"),
)
recipient_admissions = sa.Table(
    "recipient_admissions",
    metadata,
    sa.Column("operation_id", UUID, primary_key=True),
    sa.Column("day", sa.Date, primary_key=True),
    sa.Column("recipient_hash", sa.Text, nullable=False),
)
sa.Index(
    "recipient_days_expiry_idx",
    recipient_days.c.day,
    recipient_days.c.recipient_hash,
)
sa.Index(
    "recipient_admissions_expiry_idx",
    recipient_admissions.c.day,
    recipient_admissions.c.operation_id,
)
