"""Independent durable health demand; generations are allocated at freeze."""

import sqlalchemy as sa
from alembic import op

revision = "0007_health_requests"
down_revision = "0006_content_webhooks"
branch_labels = None
depends_on = None


def operations(include_health):
    op.drop_constraint(
        op.f("ck_repository_command_receipts_valid_operation"),
        "repository_command_receipts",
        type_="check",
    )
    values = (
        "'sync.refresh','sync.backfill','health.refresh'"
        if include_health
        else "'sync.refresh','sync.backfill'"
    )
    op.create_check_constraint(
        "valid_operation", "repository_command_receipts", f"operation IN ({values})"
    )


def upgrade():
    for name in ("health_requested_sequence", "health_processed_sequence"):
        op.add_column(
            "repository_syncs",
            sa.Column(name, sa.BigInteger(), nullable=False, server_default="0"),
        )
    op.add_column("repository_syncs", sa.Column("active_health_job_id", sa.Uuid()))
    op.create_foreign_key(
        "fk_repository_syncs_active_health_job_id_jobs",
        "repository_syncs",
        "jobs",
        ["active_health_job_id"],
        ["id"],
    )
    op.add_column(
        "repository_syncs",
        sa.Column(
            "health_status", sa.String(32), nullable=False, server_default="idle"
        ),
    )
    op.add_column("repository_syncs", sa.Column("health_error_code", sa.String(64)))
    op.create_check_constraint(
        "health_sequence_order",
        "repository_syncs",
        "health_requested_sequence >= health_processed_sequence AND health_processed_sequence >= 0",
    )
    op.create_check_constraint(
        "valid_health_status",
        "repository_syncs",
        "health_status IN ('idle','queued','running','retry_wait','completed','failed')",
    )
    operations(True)


def downgrade():
    if op.get_bind().scalar(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM repository_syncs WHERE health_requested_sequence > 0 OR health_generation > 0 OR active_health_job_id IS NOT NULL OR health_status <> 'idle' OR health_error_code IS NOT NULL) OR EXISTS (SELECT 1 FROM health_snapshots) OR EXISTS (SELECT 1 FROM jobs WHERE kind='repository.health') OR EXISTS (SELECT 1 FROM repository_command_receipts WHERE operation='health.refresh')"
        )
    ):
        raise RuntimeError(
            "Cannot discard phase-two data; restore a compatible backup or use forward repair"
        )
    operations(False)
    for name in ("health_sequence_order", "valid_health_status"):
        op.drop_constraint(
            op.f("ck_repository_syncs_" + name), "repository_syncs", type_="check"
        )
    op.drop_constraint(
        "fk_repository_syncs_active_health_job_id_jobs",
        "repository_syncs",
        type_="foreignkey",
    )
    for name in (
        "health_requested_sequence",
        "health_processed_sequence",
        "active_health_job_id",
        "health_status",
        "health_error_code",
    ):
        op.drop_column("repository_syncs", name)
