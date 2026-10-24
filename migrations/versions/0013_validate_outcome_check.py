"""Validate the widened decision outcome check without blocking writes.

Revision ``0010`` adds the check ``NOT VALID`` so that its exclusive
lock is brief. Validation runs here, in its own transaction, under a
SHARE UPDATE EXCLUSIVE lock that leaves reads and writes running.

Revision ID: 0013
Revises: 0012
"""

from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Scan existing rows once; new rows were checked all along."""
    op.execute(
        "ALTER TABLE decisions VALIDATE CONSTRAINT decisions_outcome_check"
    )


def downgrade() -> None:
    """A validated check stays valid; there is nothing to undo."""
