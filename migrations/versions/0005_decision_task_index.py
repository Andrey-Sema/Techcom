"""Index decisions by task for the orchestrator's history tool.

The orchestrator reads earlier actions of one task before planning a new
action. Without this index that lookup scans every decision ever kept.

Revision ID: 0005
Revises: 0004
"""

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add the task lookup index under a bounded lock wait."""
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("CREATE INDEX decisions_task_idx ON decisions(task_id, id)")


def downgrade() -> None:
    """Drop the lookup index; no data depends on it."""
    op.execute("DROP INDEX decisions_task_idx")
