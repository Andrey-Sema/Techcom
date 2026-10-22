"""Add the retention role: it may delete old rows, nothing else.

``followup_maintenance`` is a NOLOGIN group role used only by
``followup maintenance prune`` (docs/18). It can read and delete in the
tables retention covers, but cannot insert or update anything, so it
cannot forge history either; the service role keeps its append-only
audit trail (migration 0002).

Revision ID: 0003
Revises: 0002
"""

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

_UPGRADE = (
    """
    DO $$
    BEGIN
        IF NOT EXISTS (
            SELECT FROM pg_roles WHERE rolname = 'followup_maintenance'
        ) THEN
            CREATE ROLE followup_maintenance NOLOGIN;
        END IF;
        EXECUTE 'GRANT CONNECT ON DATABASE '
            || quote_ident(current_database()) || ' TO followup_maintenance';
    END
    $$
    """,
    "GRANT USAGE ON SCHEMA public TO followup_maintenance",
    (
        "GRANT SELECT, DELETE ON runs, decisions, actions, llm_cache,"
        " audit_events TO followup_maintenance"
    ),
)

_DOWNGRADE = (
    (
        "REVOKE ALL ON runs, decisions, actions, llm_cache, audit_events"
        " FROM followup_maintenance"
    ),
    "REVOKE ALL ON SCHEMA public FROM followup_maintenance",
    """
    DO $$
    BEGIN
        EXECUTE 'REVOKE ALL ON DATABASE '
            || quote_ident(current_database())
            || ' FROM followup_maintenance';
    END
    $$
    """,
)


def upgrade() -> None:
    """Create the role if missing and grant read and delete only."""
    for statement in _UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    """Revoke the grants; the cluster-wide role is kept."""
    for statement in _DOWNGRADE:
        op.execute(statement)
