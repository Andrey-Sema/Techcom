"""Grant the application role DML only; audit rows are append-only.

``followup_app`` is a NOLOGIN group role (decision D3 in ``docs/15``).
Login users for the service are created by provisioning and made
members of it; the migrator owns the schema and runs this file. The
role is created only when it is missing, so a migrator in production
needs no ``CREATEROLE`` when operations pre-provision it.

Revision ID: 0002
Revises: 0001
"""

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

_UPGRADE = (
    """
    DO $$
    BEGIN
        IF NOT EXISTS (
            SELECT FROM pg_roles WHERE rolname = 'followup_app'
        ) THEN
            CREATE ROLE followup_app NOLOGIN;
        END IF;
    END
    $$
    """,
    """
    DO $$
    BEGIN
        EXECUTE 'REVOKE ALL ON DATABASE '
            || quote_ident(current_database()) || ' FROM PUBLIC';
        EXECUTE 'GRANT CONNECT ON DATABASE '
            || quote_ident(current_database()) || ' TO followup_app';
    END
    $$
    """,
    "REVOKE ALL ON SCHEMA public FROM PUBLIC",
    "GRANT USAGE ON SCHEMA public TO followup_app",
    "GRANT SELECT, INSERT, UPDATE ON runs, decisions, actions TO followup_app",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON llm_cache TO followup_app",
    # Append-only audit trail (ASVS 16.4.2): no UPDATE/DELETE/TRUNCATE.
    "GRANT SELECT, INSERT ON audit_events TO followup_app",
    "GRANT SELECT ON alembic_version TO followup_app",
)

_DOWNGRADE = (
    (
        "REVOKE ALL ON runs, decisions, actions, llm_cache, audit_events,"
        " alembic_version FROM followup_app"
    ),
    "REVOKE ALL ON SCHEMA public FROM followup_app",
    "GRANT USAGE ON SCHEMA public TO PUBLIC",
    """
    DO $$
    BEGIN
        EXECUTE 'REVOKE ALL ON DATABASE '
            || quote_ident(current_database()) || ' FROM followup_app';
        EXECUTE 'GRANT CONNECT, TEMPORARY ON DATABASE '
            || quote_ident(current_database()) || ' TO PUBLIC';
    END
    $$
    """,
)


def upgrade() -> None:
    """Restrict PUBLIC and grant the application role its DML."""
    for statement in _UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    """Restore PostgreSQL defaults; the cluster-wide role is kept."""
    for statement in _DOWNGRADE:
        op.execute(statement)
