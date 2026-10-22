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
    sa.UniqueConstraint("run_id", "task_id"),
    sa.CheckConstraint(
        "outcome IN ('none','enqueued','duplicate_suppressed')",
        name="decisions_outcome_check",
    ),
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
    sa.CheckConstraint("attempts >= 0", name="actions_attempts_check"),
    sa.CheckConstraint(
        "(lease_until IS NULL) = (lease_token IS NULL)",
        name="actions_lease_check",
    ),
)
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
