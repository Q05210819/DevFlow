"""Repository onboarding and persistent authorization epochs."""

import sqlalchemy as sa
from alembic import op

revision = "0002_repositories"
down_revision = "0001_identity_audit"
branch_labels = None
depends_on = None


def lifecycle_columns():
    return [
        sa.Column(
            "available", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column(
            "needs_verification",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column("auth_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("verified_at", sa.DateTime(timezone=True)),
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
        sa.CheckConstraint("auth_version >= 1", name="positive_auth_version"),
    ]


def upgrade():
    op.create_table(
        "github_installations",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("installation_id", sa.BigInteger(), nullable=False, unique=True),
        sa.Column("app_id", sa.BigInteger()),
        sa.Column("account_id", sa.BigInteger()),
        sa.CheckConstraint("installation_id > 0", name="positive_installation_id"),
        *lifecycle_columns(),
    )
    op.create_table(
        "repositories",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("github_repository_id", sa.BigInteger(), nullable=False, unique=True),
        sa.Column(
            "installation_id",
            sa.BigInteger(),
            sa.ForeignKey("github_installations.installation_id"),
            nullable=False,
        ),
        sa.Column("full_name", sa.String(512), nullable=False),
        sa.Column("private", sa.Boolean(), nullable=False),
        sa.Column("default_branch", sa.String(512)),
        sa.Column(
            "enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")
        ),
        sa.CheckConstraint("github_repository_id > 0", name="positive_github_id"),
        *lifecycle_columns(),
    )
    op.create_index(
        "ix_repositories_installation_id", "repositories", ["installation_id"]
    )
    op.create_index(
        "ix_repositories_created_at_id", "repositories", ["created_at", "id"]
    )


def downgrade():
    op.drop_table("repositories")
    op.drop_table("github_installations")
