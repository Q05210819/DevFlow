"""Immutable authorized versions and durable physical-operation exclusion."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision = "0009_workspace"
down_revision = "0008_sync_repair"
branch_labels = None
depends_on = None


def upgrade():
    op.drop_constraint(op.f("ck_jobs_valid_kind"), "jobs", type_="check")
    op.drop_constraint(op.f("ck_jobs_valid_creator"), "jobs", type_="check")
    op.create_check_constraint(
        "valid_kind",
        "jobs",
        "kind IN ('repository.access_probe','repository.sync','repository.health','repository.workspace')",
    )
    op.create_check_constraint(
        "valid_creator",
        "jobs",
        "(kind = 'repository.access_probe' AND requested_by IS NOT NULL) OR (kind IN ('repository.sync','repository.health','repository.workspace') AND requested_by IS NULL)",
    )
    op.create_table(
        "workspace_versions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "repository_id", sa.Uuid(), sa.ForeignKey("repositories.id"), nullable=False
        ),
        sa.Column(
            "source_repository_id",
            sa.Uuid(),
            sa.ForeignKey("repositories.id"),
            nullable=False,
        ),
        sa.Column("cache_repository_id", sa.BigInteger(), nullable=False),
        sa.Column("requested_by", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("input_hash", sa.String(64), nullable=False),
        sa.Column("selection", pg.JSONB(), nullable=False),
        sa.Column("source", pg.JSONB(), nullable=False),
        sa.Column("scopes", pg.JSONB(), nullable=False),
        sa.Column("commit_sha", sa.String(40), nullable=False),
        sa.Column("tree_oid", sa.String(40), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="queued"),
        sa.Column("pin_ref", sa.String(128)),
        sa.Column(
            "job_id", sa.Uuid(), sa.ForeignKey("jobs.id"), nullable=False, unique=True
        ),
        sa.Column("error", sa.String(64)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.clock_timestamp(),
        ),
        sa.Column(
            "last_used_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.clock_timestamp(),
        ),
        sa.UniqueConstraint(
            "requested_by",
            "repository_id",
            "idempotency_key",
            name="uq_workspace_request",
        ),
        sa.CheckConstraint(
            "commit_sha ~ '^[0-9a-f]{40}$' AND tree_oid ~ '^[0-9a-f]{40}$'",
            name="object_ids",
        ),
        sa.CheckConstraint(
            "status IN ('queued','ready','failed','unavailable')", name="status"
        ),
        sa.CheckConstraint(
            "idempotency_key ~ '^[!-~]{1,128}$' AND input_hash ~ '^[0-9a-f]{64}$'",
            name="request_identity",
        ),
    )
    op.create_index(
        "ix_workspace_repository_created",
        "workspace_versions",
        ["repository_id", "created_at", "id"],
    )
    op.create_table(
        "workspace_process_guards",
        sa.Column("operation_id", sa.String(32), primary_key=True),
        sa.Column("nonce", sa.String(32), nullable=False),
        sa.Column("repository_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "workspace_id",
            sa.Uuid(),
            sa.ForeignKey("workspace_versions.id"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(8), nullable=False),
        sa.Column("state", sa.String(24), nullable=False, server_default="reserved"),
        sa.Column("runtime_id", sa.Uuid(), nullable=False),
        sa.Column("lock_path", sa.String(4096), nullable=False),
        sa.Column("deadline", sa.Float(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("scopes", pg.JSONB(), nullable=False),
        sa.Column("actor", pg.JSONB()),
        sa.Column("lease", pg.JSONB()),
        sa.Column("identity", pg.JSONB()),
        sa.Column("proof", sa.String(32)),
        sa.Column("drained_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "operation_id ~ '^[0-9a-f]{32}$' AND nonce ~ '^[0-9a-f]{32}$'",
            name="identity",
        ),
        sa.CheckConstraint("kind IN ('read','write','gc')", name="kind"),
        sa.CheckConstraint(
            "state IN ('reserved','active','cleanup_required','drained')", name="state"
        ),
        sa.CheckConstraint("repository_id > 0", name="repository"),
    )
    op.create_index(
        "ix_workspace_guards_admission",
        "workspace_process_guards",
        ["state", "kind", "repository_id"],
    )


def downgrade():
    if op.get_bind().scalar(
        sa.text(
            "SELECT EXISTS(SELECT 1 FROM workspace_versions) OR EXISTS(SELECT 1 FROM workspace_process_guards WHERE state != 'drained')"
        )
    ):
        raise RuntimeError(
            "Cannot discard workspace provenance or unresolved physical guards"
        )
    op.drop_table("workspace_process_guards")
    op.drop_table("workspace_versions")
    op.drop_constraint(op.f("ck_jobs_valid_kind"), "jobs", type_="check")
    op.drop_constraint(op.f("ck_jobs_valid_creator"), "jobs", type_="check")
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
