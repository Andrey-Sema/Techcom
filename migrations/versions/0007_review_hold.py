"""Let a planned action wait for a person before it can be sent.

The orchestrator may ask for a human review instead of sending. Such an
action is stored as ``held``: dispatchers claim only ``pending`` rows,
so nothing sends it until an operator approves it (``held`` to
``pending``) or rejects it (``held`` to ``dead``).

PostgreSQL cannot drop an enum value. A downgrade therefore refuses
while held actions exist and leaves the unused value in place; a later
upgrade adds it idempotently.

Revision ID: 0007
Revises: 0006
"""

from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add the review state to the action status type."""
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("ALTER TYPE action_status ADD VALUE IF NOT EXISTS 'held'")


def downgrade() -> None:
    """Refuse while any action still waits for a review."""
    op.execute("""
    DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM actions WHERE status::text = 'held') THEN
            RAISE EXCEPTION 'held actions prevent downgrade';
        END IF;
    END $$;
    """)
