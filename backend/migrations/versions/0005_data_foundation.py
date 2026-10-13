"""Versioned facts, durable sync demand and system job identities."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0005_data_foundation"
down_revision = "0004_jobs_events"
branch_labels = None
depends_on = None


def upgrade():
    op.drop_constraint(op.f("ck_jobs_valid_kind"), "jobs", type_="check")
    op.alter_column("jobs", "requested_by", existing_type=sa.Uuid(), nullable=True)
    op.add_column(
        "jobs",
        sa.Column("request_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column("jobs", sa.Column("attempt_started_at", sa.DateTime(timezone=True)))
    op.create_check_constraint(
        "valid_kind",
        "jobs",
        "kind IN ('repository.access_probe', 'repository.sync', 'repository.health')",
    )
    op.create_check_constraint(
        "valid_creator",
        "jobs",
        "(kind = 'repository.access_probe' AND requested_by IS NOT NULL) OR (kind IN ('repository.sync', 'repository.health') AND requested_by IS NULL)",
    )
    op.create_check_constraint("request_count", "jobs", "request_count >= 0")
    op.create_index(
        "uq_jobs_system_repository_kind_key",
        "jobs",
        ["repository_id", "kind", "idempotency_key"],
        unique=True,
        postgresql_where=sa.text("requested_by IS NULL"),
    )
    op.create_table(
        "facts",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "repository_id", sa.Uuid(), sa.ForeignKey("repositories.id"), nullable=False
        ),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("source_id", sa.BigInteger(), nullable=False),
        sa.Column("version_key", sa.String(256), nullable=False, server_default=""),
        sa.Column("number", sa.BigInteger()),
        sa.Column("parent_kind", sa.String(16)),
        sa.Column("parent_source_id", sa.BigInteger()),
        sa.Column("state", sa.String(32)),
        sa.Column("source_updated_at", sa.DateTime(timezone=True)),
        sa.Column(
            "observed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.clock_timestamp(),
        ),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column(
            "unavailable", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.UniqueConstraint(
            "repository_id",
            "kind",
            "source_id",
            "version_key",
            name="uq_facts_source_version",
        ),
        sa.CheckConstraint(
            "kind IN ('issue','pull','comment','review','review_comment','pr_file','ci_run','ci_job')",
            name="valid_kind",
        ),
        sa.CheckConstraint(
            "source_id > 0 AND (number IS NULL OR number > 0) AND (parent_source_id IS NULL OR parent_source_id > 0)",
            name="positive_source_ids",
        ),
        sa.CheckConstraint(
            "(parent_kind IS NULL) = (parent_source_id IS NULL)", name="paired_parent"
        ),
        sa.CheckConstraint(
            "COALESCE((kind IN ('issue','pull','ci_run') AND parent_kind IS NULL) OR (kind='comment' AND parent_kind IN ('issue','pull')) OR (kind IN ('review','review_comment','pr_file') AND parent_kind='pull') OR (kind='ci_job' AND parent_kind='ci_run'), false)",
            name="valid_parent",
        ),
    )
    op.create_index(
        "ix_facts_repository_kind_updated_id",
        "facts",
        ["repository_id", "kind", "source_updated_at", "id"],
    )
    op.create_index(
        "ix_facts_parent",
        "facts",
        ["repository_id", "parent_kind", "parent_source_id", "kind", "version_key"],
    )
    op.create_index(
        "ix_facts_repository_kind_number", "facts", ["repository_id", "kind", "number"]
    )
    op.create_table(
        "fact_collections",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "repository_id", sa.Uuid(), sa.ForeignKey("repositories.id"), nullable=False
        ),
        sa.Column("parent_kind", sa.String(16), nullable=False),
        sa.Column("parent_source_id", sa.BigInteger(), nullable=False),
        sa.Column("collection", sa.String(32), nullable=False),
        sa.Column("version_key", sa.String(256), nullable=False, server_default=""),
        sa.Column("complete", sa.Boolean(), nullable=False),
        sa.Column("reason", sa.String(64)),
        sa.Column(
            "observed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.clock_timestamp(),
        ),
        sa.UniqueConstraint(
            "repository_id",
            "parent_kind",
            "parent_source_id",
            "collection",
            "version_key",
            name="uq_fact_collections_parent_version",
        ),
        sa.CheckConstraint("parent_source_id > 0", name="positive_parent_id"),
        sa.CheckConstraint(
            "(parent_kind='issue' AND collection='comments') OR (parent_kind='pull' AND collection IN ('comments','reviews','review_comments','files')) OR (parent_kind='ci_run' AND collection IN ('attempts','jobs'))",
            name="valid_collection",
        ),
        sa.CheckConstraint(
            "(complete AND reason IS NULL) OR (NOT complete AND reason IS NOT NULL)",
            name="complete_reason",
        ),
    )
    op.create_table(
        "health_snapshots",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "repository_id", sa.Uuid(), sa.ForeignKey("repositories.id"), nullable=False
        ),
        sa.Column("generation", sa.BigInteger(), nullable=False),
        sa.Column("rule_version", sa.String(64), nullable=False),
        sa.Column(
            "generated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.clock_timestamp(),
        ),
        sa.Column("manifest", postgresql.JSONB(), nullable=False),
        sa.Column("result", postgresql.JSONB(), nullable=False),
        sa.UniqueConstraint(
            "repository_id",
            "generation",
            name="uq_health_snapshots_repository_generation",
        ),
        sa.CheckConstraint("generation >= 1", name="positive_generation"),
    )
    op.create_table(
        "repository_syncs",
        sa.Column(
            "repository_id",
            sa.Uuid(),
            sa.ForeignKey("repositories.id"),
            primary_key=True,
        ),
        sa.Column("generation", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column(
            "requested_sequence", sa.BigInteger(), nullable=False, server_default="0"
        ),
        sa.Column(
            "processed_sequence", sa.BigInteger(), nullable=False, server_default="0"
        ),
        sa.Column("active_job_id", sa.Uuid(), sa.ForeignKey("jobs.id")),
        sa.Column("status", sa.String(32), nullable=False, server_default="idle"),
        sa.Column("round_started_at", sa.DateTime(timezone=True)),
        sa.Column("request_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_due_at", sa.DateTime(timezone=True)),
        sa.Column("last_reconciled_at", sa.DateTime(timezone=True)),
        sa.Column(
            "health_generation", sa.BigInteger(), nullable=False, server_default="0"
        ),
        sa.Column(
            "published_health_generation",
            sa.BigInteger(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("current_health_id", sa.Uuid(), sa.ForeignKey("health_snapshots.id")),
        sa.CheckConstraint("generation >= 0", name="generation"),
        sa.CheckConstraint(
            "requested_sequence >= processed_sequence AND processed_sequence >= 0",
            name="sequence_order",
        ),
        sa.CheckConstraint(
            "health_generation >= published_health_generation AND published_health_generation >= 0",
            name="health_generation_order",
        ),
        sa.CheckConstraint("request_count >= 0", name="request_count"),
        sa.CheckConstraint(
            "status IN ('idle','queued','running','retry_wait','completed','failed','budget_exhausted')",
            name="valid_status",
        ),
    )
    op.create_index(
        "ix_repository_syncs_next_due",
        "repository_syncs",
        ["next_due_at", "repository_id"],
    )
    op.create_table(
        "sync_cursors",
        sa.Column(
            "repository_id",
            sa.Uuid(),
            sa.ForeignKey("repositories.id"),
            primary_key=True,
        ),
        sa.Column("stream", sa.String(16), primary_key=True),
        sa.Column("checkpoint", postgresql.JSONB(), nullable=False),
        sa.Column("covered_from", sa.DateTime(timezone=True)),
        sa.Column("covered_until", sa.DateTime(timezone=True)),
        sa.Column("completed_watermark", sa.DateTime(timezone=True)),
        sa.Column("status", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("error_code", sa.String(64)),
        sa.Column("last_success_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "stream IN ('issues','pulls','actions')", name="valid_stream"
        ),
        sa.CheckConstraint(
            "status IN ('pending','running','complete','partial','failed','permission_missing')",
            name="valid_status",
        ),
        sa.CheckConstraint(
            "covered_from IS NULL OR covered_until IS NULL OR covered_from <= covered_until",
            name="ordered_coverage",
        ),
    )
    op.create_table(
        "sync_demands",
        sa.Column(
            "repository_id",
            sa.Uuid(),
            sa.ForeignKey("repositories.id"),
            primary_key=True,
        ),
        sa.Column("sequence", sa.BigInteger(), primary_key=True),
        sa.Column("reason", sa.String(32), nullable=False),
        sa.Column("target", postgresql.JSONB(none_as_null=True)),
        sa.Column("backfill", postgresql.JSONB(none_as_null=True)),
        sa.CheckConstraint("sequence >= 1", name="positive_sequence"),
        sa.CheckConstraint(
            "reason IN ('registration','refresh','webhook','periodic','reconcile','backfill')",
            name="valid_reason",
        ),
        sa.CheckConstraint(
            "(reason='backfill') = (backfill IS NOT NULL)", name="backfill_reason"
        ),
    )


def downgrade():
    # A rollback must never silently erase fact evidence or active system jobs.
    occupied = op.get_bind().scalar(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM facts) OR EXISTS (SELECT 1 FROM fact_collections) OR EXISTS (SELECT 1 FROM health_snapshots) OR EXISTS (SELECT 1 FROM repository_syncs) OR EXISTS (SELECT 1 FROM sync_cursors) OR EXISTS (SELECT 1 FROM sync_demands) OR EXISTS (SELECT 1 FROM jobs WHERE requested_by IS NULL OR request_count <> 0 OR attempt_started_at IS NOT NULL)"
        )
    )
    if occupied:
        raise RuntimeError(
            "Cannot downgrade existing phase-two data; restore a compatible backup or fix forward"
        )
    for table in (
        "sync_demands",
        "sync_cursors",
        "repository_syncs",
        "health_snapshots",
        "fact_collections",
        "facts",
    ):
        op.drop_table(table)
    op.drop_index("uq_jobs_system_repository_kind_key", table_name="jobs")
    op.drop_constraint(op.f("ck_jobs_valid_creator"), "jobs", type_="check")
    op.drop_constraint(op.f("ck_jobs_request_count"), "jobs", type_="check")
    op.drop_constraint(op.f("ck_jobs_valid_kind"), "jobs", type_="check")
    op.drop_column("jobs", "attempt_started_at")
    op.drop_column("jobs", "request_count")
    op.alter_column("jobs", "requested_by", existing_type=sa.Uuid(), nullable=False)
    op.create_check_constraint("valid_kind", "jobs", "kind = 'repository.access_probe'")
