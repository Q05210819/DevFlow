"""Durable access-check jobs and ordered status events."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0004_jobs_events"
down_revision = "0003_webhook_deliveries"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "jobs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "kind",
            sa.String(64),
            nullable=False,
            server_default="repository.access_probe",
        ),
        sa.Column(
            "repository_id", sa.Uuid(), sa.ForeignKey("repositories.id"), nullable=False
        ),
        sa.Column("requested_by", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("input", postgresql.JSONB(), nullable=False),
        sa.Column("input_hash", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="queued"),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="3"),
        sa.Column(
            "available_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.clock_timestamp(),
        ),
        sa.Column("lease_owner", sa.Uuid()),
        sa.Column(
            "lease_generation", sa.BigInteger(), nullable=False, server_default="0"
        ),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("result", postgresql.JSONB()),
        sa.Column("error_code", sa.String(64)),
        sa.Column("last_sequence", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.clock_timestamp(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.clock_timestamp(),
        ),
        sa.UniqueConstraint(
            "requested_by", "kind", "idempotency_key", name="uq_jobs_creator_kind_key"
        ),
        sa.CheckConstraint("kind = 'repository.access_probe'", name="valid_kind"),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'retry_wait', 'succeeded', 'failed')",
            name="valid_status",
        ),
        sa.CheckConstraint(
            "attempt >= 0 AND attempt <= max_attempts AND max_attempts BETWEEN 1 AND 3",
            name="attempt_budget",
        ),
        sa.CheckConstraint("lease_generation >= 0", name="lease_generation"),
        sa.CheckConstraint("last_sequence >= 0", name="last_sequence"),
        sa.CheckConstraint(
            "idempotency_key ~ '^[!-~]{1,128}$'", name="idempotency_key"
        ),
        sa.CheckConstraint("input_hash ~ '^[0-9a-f]{64}$'", name="input_hash"),
    )
    op.create_index("ix_jobs_repository_id", "jobs", ["repository_id"])
    op.create_index("ix_jobs_requested_by", "jobs", ["requested_by"])
    op.create_index(
        "ix_jobs_due", "jobs", ["status", "available_at", "created_at", "id"]
    )
    op.create_index(
        "ix_jobs_expired_lease", "jobs", ["status", "lease_expires_at", "id"]
    )
    op.create_table(
        "run_events",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "job_id",
            sa.Uuid(),
            sa.ForeignKey("jobs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("sequence", sa.BigInteger(), nullable=False),
        sa.Column("event", sa.String(32), nullable=False, server_default="job.status"),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.clock_timestamp(),
        ),
        sa.UniqueConstraint("job_id", "sequence", name="uq_run_events_job_sequence"),
        sa.CheckConstraint("sequence >= 1", name="positive_sequence"),
        sa.CheckConstraint("event = 'job.status'", name="valid_event"),
    )


def downgrade():
    op.drop_table("run_events")
    op.drop_table("jobs")
