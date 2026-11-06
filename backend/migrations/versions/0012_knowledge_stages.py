"""Durable HTTP upload staging identity; no second copy of canonical bytes."""

import sqlalchemy as sa
from alembic import op

revision = "0012_knowledge_stages"
down_revision = "0011_knowledge_imports"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("knowledge_upload_stages", sa.Column("repository_id", sa.Uuid()))
    op.add_column("knowledge_upload_stages", sa.Column("purpose", sa.String(16)))
    op.add_column(
        "knowledge_upload_stages", sa.Column("idempotency_key", sa.String(128))
    )
    op.add_column("knowledge_upload_stages", sa.Column("request_hash", sa.String(64)))
    op.add_column("knowledge_upload_stages", sa.Column("document_id", sa.Uuid()))
    op.add_column("knowledge_upload_stages", sa.Column("body_bytes", sa.Integer()))
    op.create_foreign_key(
        "fk_knowledge_upload_stages_repository_id_repositories",
        "knowledge_upload_stages",
        "repositories",
        ["repository_id"],
        ["id"],
    )
    op.create_foreign_key(
        "fk_knowledge_upload_stages_document_id_knowledge_documents",
        "knowledge_upload_stages",
        "knowledge_documents",
        ["document_id"],
        ["id"],
    )
    op.create_check_constraint(
        "purpose",
        "knowledge_upload_stages",
        "purpose IS NULL OR purpose IN ('new','replace')",
    )
    op.create_check_constraint(
        "keyed",
        "knowledge_upload_stages",
        "(idempotency_key IS NULL AND request_hash IS NULL AND purpose IS NULL"
        " AND repository_id IS NULL AND body_bytes IS NULL)"
        " OR (idempotency_key IS NOT NULL AND request_hash ~ '^[0-9a-f]{64}$'"
        " AND purpose IS NOT NULL AND repository_id IS NOT NULL"
        " AND body_bytes BETWEEN 0 AND 1048576"
        " AND (purpose='new' OR document_id IS NOT NULL))",
    )
    # Keyed HTTP stages are unique per actor/repository/key; historical and
    # internal unkeyed rows keep the NULL exemption.
    op.create_index(
        "uq_knowledge_stage_request",
        "knowledge_upload_stages",
        ["actor_id", "repository_id", "idempotency_key"],
        unique=True,
        postgresql_where=sa.text("idempotency_key IS NOT NULL"),
    )


def downgrade():
    keyed = op.get_bind().scalar(
        sa.text(
            "SELECT EXISTS(SELECT 1 FROM knowledge_upload_stages"
            " WHERE idempotency_key IS NOT NULL)"
        )
    )
    if keyed:
        raise RuntimeError("Cannot discard keyed upload stage replays")
    op.drop_index(
        "uq_knowledge_stage_request",
        table_name="knowledge_upload_stages",
        postgresql_where=sa.text("idempotency_key IS NOT NULL"),
    )
    op.drop_constraint(
        op.f("ck_knowledge_upload_stages_keyed"),
        "knowledge_upload_stages",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_knowledge_upload_stages_purpose"),
        "knowledge_upload_stages",
        type_="check",
    )
    op.drop_constraint(
        "fk_knowledge_upload_stages_document_id_knowledge_documents",
        "knowledge_upload_stages",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_knowledge_upload_stages_repository_id_repositories",
        "knowledge_upload_stages",
        type_="foreignkey",
    )
    for name in (
        "body_bytes",
        "document_id",
        "request_hash",
        "idempotency_key",
        "purpose",
        "repository_id",
    ):
        op.drop_column("knowledge_upload_stages", name)
