"""Finite content receipts, durable command idempotency and version hints."""

import sqlalchemy as sa
from alembic import op

revision = "0006_content_webhooks"
down_revision = "0005_data_foundation"
branch_labels = None
depends_on = None


def upgrade():
    op.drop_constraint(
        op.f("ck_webhook_deliveries_valid_event"), "webhook_deliveries", type_="check"
    )
    op.drop_constraint(
        op.f("ck_webhook_deliveries_valid_action"), "webhook_deliveries", type_="check"
    )
    op.create_check_constraint(
        "valid_event",
        "webhook_deliveries",
        "event_name IN ('installation','installation_repositories','github_app_authorization','unknown','issues','issue_comment','pull_request','pull_request_review','pull_request_review_comment','workflow_run','workflow_job','push','create','delete','repository')",
    )
    op.create_check_constraint(
        "valid_action",
        "webhook_deliveries",
        "action IS NULL OR action IN ('created','deleted','suspend','unsuspend','new_permissions_accepted','added','removed','revoked','opened','edited','closed','reopened','assigned','unassigned','labeled','unlabeled','locked','unlocked','milestoned','demilestoned','pinned','unpinned','transferred','typed','untyped','synchronize','converted_to_draft','ready_for_review','review_requested','review_request_removed','enqueued','dequeued','auto_merge_enabled','auto_merge_disabled','submitted','dismissed','requested','in_progress','completed','queued','waiting','pending','renamed','privatized','publicized','archived','unarchived')",
    )
    for name in ("github_repository_id", "source_id", "number", "attempt"):
        op.add_column("webhook_deliveries", sa.Column(name, sa.BigInteger()))
    op.add_column("webhook_deliveries", sa.Column("target_kind", sa.String(16)))
    op.add_column(
        "repository_syncs",
        sa.Column(
            "version_hint_sequence", sa.BigInteger(), nullable=False, server_default="0"
        ),
    )
    op.create_check_constraint(
        "version_hint_sequence", "repository_syncs", "version_hint_sequence >= 0"
    )
    op.create_table(
        "repository_command_receipts",
        sa.Column("actor_id", sa.Uuid(), sa.ForeignKey("users.id"), primary_key=True),
        sa.Column(
            "repository_id",
            sa.Uuid(),
            sa.ForeignKey("repositories.id"),
            primary_key=True,
        ),
        sa.Column("operation", sa.String(32), primary_key=True),
        sa.Column("idempotency_key", sa.String(128), primary_key=True),
        sa.Column("payload_hash", sa.String(64), nullable=False),
        sa.Column("demand_sequence", sa.BigInteger(), nullable=False),
        sa.CheckConstraint(
            "operation IN ('sync.refresh','sync.backfill')", name="valid_operation"
        ),
        sa.CheckConstraint("demand_sequence > 0", name="positive_demand_sequence"),
        sa.CheckConstraint("payload_hash ~ '^[0-9a-f]{64}$'", name="payload_hash"),
        sa.CheckConstraint(
            "idempotency_key ~ '^[!-~]{1,128}$'", name="idempotency_key"
        ),
    )


def downgrade():
    occupied = op.get_bind().scalar(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM repository_command_receipts) "
            "OR EXISTS (SELECT 1 FROM webhook_deliveries WHERE event_name NOT IN ('installation','installation_repositories','github_app_authorization','unknown') OR github_repository_id IS NOT NULL OR target_kind IS NOT NULL OR source_id IS NOT NULL OR number IS NOT NULL OR attempt IS NOT NULL) "
            "OR EXISTS (SELECT 1 FROM facts) OR EXISTS (SELECT 1 FROM fact_collections) "
            "OR EXISTS (SELECT 1 FROM health_snapshots) OR EXISTS (SELECT 1 FROM repository_syncs) "
            "OR EXISTS (SELECT 1 FROM sync_cursors) OR EXISTS (SELECT 1 FROM sync_demands) "
            "OR EXISTS (SELECT 1 FROM jobs WHERE requested_by IS NULL OR request_count <> 0 OR attempt_started_at IS NOT NULL)"
        )
    )
    if occupied:
        raise RuntimeError(
            "Cannot discard phase-two data; restore a compatible backup or use forward repair"
        )
    op.drop_table("repository_command_receipts")
    op.drop_constraint(
        op.f("ck_repository_syncs_version_hint_sequence"),
        "repository_syncs",
        type_="check",
    )
    op.drop_column("repository_syncs", "version_hint_sequence")
    for name in (
        "github_repository_id",
        "source_id",
        "number",
        "attempt",
        "target_kind",
    ):
        op.drop_column("webhook_deliveries", name)
    op.drop_constraint(
        op.f("ck_webhook_deliveries_valid_event"), "webhook_deliveries", type_="check"
    )
    op.drop_constraint(
        op.f("ck_webhook_deliveries_valid_action"), "webhook_deliveries", type_="check"
    )
    op.create_check_constraint(
        "valid_event",
        "webhook_deliveries",
        "event_name IN ('installation','installation_repositories','github_app_authorization','unknown')",
    )
    op.create_check_constraint(
        "valid_action",
        "webhook_deliveries",
        "action IS NULL OR action IN ('created','deleted','suspend','unsuspend','new_permissions_accepted','added','removed','revoked')",
    )
