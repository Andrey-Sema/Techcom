"""Create the PostgreSQL 18 outbox and audit trail.

Revision ID: 0001
Revises: none
"""

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

_DDL = """
CREATE TYPE action_status AS ENUM
    ('pending', 'in_flight', 'unknown', 'sent', 'dead');
CREATE TABLE runs (
    id uuid PRIMARY KEY DEFAULT uuidv7(),
    as_of date NOT NULL,
    policy_version text NOT NULL,
    started_at timestamptz NOT NULL,
    finished_at timestamptz,
    stats jsonb NOT NULL DEFAULT '{}'
);
CREATE TABLE decisions (
    id uuid PRIMARY KEY DEFAULT uuidv7(),
    run_id uuid NOT NULL REFERENCES runs(id),
    task_id text NOT NULL,
    data jsonb NOT NULL,
    state_fingerprint text NOT NULL,
    outcome text NOT NULL,
    UNIQUE (run_id, task_id),
    CONSTRAINT decisions_outcome_check CHECK
        (outcome IN ('none', 'enqueued', 'duplicate_suppressed'))
);
CREATE TABLE actions (
    id uuid PRIMARY KEY DEFAULT uuidv7(),
    idempotency_key text NOT NULL UNIQUE,
    operation_id uuid NOT NULL UNIQUE,
    decision_id uuid NOT NULL REFERENCES decisions(id),
    status action_status NOT NULL DEFAULT 'pending',
    attempts integer NOT NULL DEFAULT 0,
    next_attempt_at timestamptz NOT NULL,
    lease_until timestamptz,
    lease_token uuid,
    last_error varchar(200),
    provider_ref text,
    sent_at timestamptz,
    CONSTRAINT actions_attempts_check CHECK (attempts >= 0),
    CONSTRAINT actions_lease_check CHECK
        ((lease_until IS NULL) = (lease_token IS NULL))
);
CREATE INDEX actions_due_idx ON actions (status, next_attempt_at)
    WHERE status IN ('pending', 'unknown');
CREATE INDEX actions_expired_idx ON actions (lease_until)
    WHERE status = 'in_flight';
CREATE TABLE llm_cache (
    cache_key text PRIMARY KEY,
    data jsonb NOT NULL,
    expires_at timestamptz NOT NULL
);
CREATE TABLE audit_events (
    id uuid PRIMARY KEY DEFAULT uuidv7(),
    kind text NOT NULL,
    at timestamptz NOT NULL,
    payload jsonb NOT NULL,
    run_id uuid,
    task_id text
);
"""


def upgrade() -> None:
    """Execute frozen DDL independent of application metadata."""
    for statement in _DDL.strip().removesuffix(";").split(";"):
        op.execute(statement)


def downgrade() -> None:
    """Remove tables in dependency order and release the enum type."""
    for name in ("audit_events", "llm_cache", "actions", "decisions", "runs"):
        op.drop_table(name)
    op.execute("DROP TYPE action_status")
