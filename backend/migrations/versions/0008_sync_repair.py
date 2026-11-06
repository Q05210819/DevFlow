"""Separate daily repair attempt deadlines from successful reconciliation evidence."""

import sqlalchemy as sa
from alembic import op

revision = "0008_sync_repair"
down_revision = "0007_health_requests"
branch_labels = None
depends_on = None


def upgrade():
    # NULL schedules one conservative full reconciliation for existing repositories.
    op.add_column(
        "repository_syncs",
        sa.Column("next_reconcile_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade():
    if op.get_bind().scalar(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM repository_syncs WHERE next_reconcile_at IS NOT NULL)"
        )
    ):
        raise RuntimeError(
            "Cannot discard sync repair deadlines; restore a compatible backup or use forward repair"
        )
    op.drop_column("repository_syncs", "next_reconcile_at")
