"""Atomic authenticated lifecycle delivery receipts."""

import sqlalchemy as sa
from alembic import op

revision = "0003_webhook_deliveries"
down_revision = "0002_repositories"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "webhook_deliveries",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("delivery_id", sa.Uuid(), nullable=False, unique=True),
        sa.Column("event_name", sa.String(32), nullable=False),
        sa.Column("action", sa.String(32)),
        sa.Column("installation_id", sa.BigInteger()),
        sa.Column("github_user_id", sa.BigInteger()),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.clock_timestamp(),
        ),
        sa.CheckConstraint("status IN ('processed', 'ignored')", name="valid_status"),
        sa.CheckConstraint(
            "event_name IN ('installation', 'installation_repositories', 'github_app_authorization', 'unknown')",
            name="valid_event",
        ),
        sa.CheckConstraint(
            "action IS NULL OR action IN ('created', 'deleted', 'suspend', 'unsuspend', 'new_permissions_accepted', 'added', 'removed', 'revoked')",
            name="valid_action",
        ),
    )


def downgrade():
    op.drop_table("webhook_deliveries")
