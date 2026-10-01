"""Identity, browser login state, credentials and controlled audit records."""

import sqlalchemy as sa
from alembic import op

revision = "0001_identity_audit"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("github_id", sa.BigInteger(), nullable=False),
        sa.Column("login", sa.String(255)),
        sa.Column("avatar_url", sa.Text()),
        sa.Column("role", sa.String(16), nullable=False, server_default="member"),
        sa.Column(
            "enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")
        ),
        sa.Column("auth_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "auth_changed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.clock_timestamp(),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("github_id"),
        sa.CheckConstraint("role IN ('admin', 'member')", name="valid_role"),
        sa.CheckConstraint("github_id > 0", name="positive_github_id"),
        sa.CheckConstraint("auth_version >= 1", name="positive_auth_version"),
    )
    op.create_table(
        "oauth_states",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("state_hash", sa.String(64), nullable=False),
        sa.Column("browser_nonce_hash", sa.String(64), nullable=False),
        sa.Column("pkce_verifier_encrypted", sa.LargeBinary(), nullable=False),
        sa.Column("return_path", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("state_hash"),
    )
    op.create_index("ix_oauth_states_expires_at", "oauth_states", ["expires_at"])
    op.create_table(
        "sessions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "user_id",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("auth_version", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("token_hash"),
    )
    op.create_index("ix_sessions_user_id", "sessions", ["user_id"])
    op.create_index("ix_sessions_expires_at", "sessions", ["expires_at"])
    op.create_table(
        "github_credentials",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "user_id",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("access_token_encrypted", sa.LargeBinary()),
        sa.Column("refresh_token_encrypted", sa.LargeBinary()),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column("refresh_expires_at", sa.DateTime(timezone=True)),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "needs_reauth",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("user_id"),
        sa.CheckConstraint("version >= 1", name="positive_version"),
    )
    op.create_table(
        "audit_logs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "actor_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="SET NULL")
        ),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("resource_type", sa.String(32), nullable=False),
        sa.Column("resource_id", sa.Uuid()),
        sa.Column("outcome", sa.String(16), nullable=False),
        sa.Column("request_id", sa.Uuid()),
        sa.Column("job_id", sa.Uuid()),
        sa.Column("reason_code", sa.String(64)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint("outcome IN ('success', 'failure')", name="valid_outcome"),
    )
    op.create_index("ix_audit_logs_created_at_id", "audit_logs", ["created_at", "id"])


def downgrade():
    for table in (
        "audit_logs",
        "github_credentials",
        "sessions",
        "oauth_states",
        "users",
    ):
        op.drop_table(table)
