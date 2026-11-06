"""Composed accepted targets and durable finite ingestion progress."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0011_knowledge_imports"
down_revision = "0010_knowledge_sources"
branch_labels = None
depends_on = None


def upgrade():
    op.drop_constraint(
        op.f("ck_knowledge_imports_kind"), "knowledge_imports", type_="check"
    )
    op.create_check_constraint(
        "kind", "knowledge_imports", "kind IN ('upload','workspace','delete')"
    )
    for column in (
        sa.Column(
            "composition_version", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column(
            "target_workspace_id", sa.Uuid(), sa.ForeignKey("workspace_versions.id")
        ),
        sa.Column("target_sha", sa.String(40)),
        sa.Column(
            "target_path_scopes",
            postgresql.JSONB(),
            nullable=False,
            server_default="{}",
        ),
        sa.Column("uploads_after", sa.Uuid()),
        sa.Column(
            "uploads_done", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column("workspace_cursor", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "workspace_done", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column("eligible_files", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("processed_chunks", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("scan_reason", sa.String(64)),
        sa.Column("tokenizer_fingerprint", sa.String(64)),
        sa.Column("chunking_version", sa.String(128)),
    ):
        op.add_column("knowledge_imports", column)
    op.create_check_constraint(
        "composition",
        "knowledge_imports",
        "composition_version IN (0,1) AND workspace_cursor >= 0 AND eligible_files BETWEEN 0 AND 2000 AND processed_chunks >= 0",
    )
    op.create_check_constraint(
        "target_metadata",
        "knowledge_imports",
        "octet_length(target_path_scopes::text) <= 8192",
    )
    op.add_column("knowledge_documents", sa.Column("accepted_revision_id", sa.Uuid()))
    op.create_unique_constraint(
        "uq_knowledge_revision_document", "knowledge_revisions", ["document_id", "id"]
    )
    op.create_foreign_key(
        "fk_knowledge_document_accepted",
        "knowledge_documents",
        "knowledge_revisions",
        ["id", "accepted_revision_id"],
        ["document_id", "id"],
    )
    op.create_check_constraint(
        "accepted",
        "knowledge_documents",
        "accepted_revision_id IS NULL OR (kind='upload' AND NOT deleted)",
    )
    op.add_column(
        "knowledge_import_members",
        sa.Column(
            "member_kind", sa.String(16), nullable=False, server_default="upload"
        ),
    )
    op.execute(
        "UPDATE knowledge_import_members m SET member_kind=i.kind FROM knowledge_imports i WHERE m.import_id=i.id AND i.kind='workspace'"
    )
    op.add_column(
        "knowledge_import_members",
        sa.Column("next_chunk", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column("knowledge_import_members", sa.Column("total_chunks", sa.Integer()))
    op.add_column("knowledge_import_members", sa.Column("declared_bytes", sa.Integer()))
    op.add_column("knowledge_import_members", sa.Column("blob_oid", sa.String(40)))
    op.drop_constraint(
        "uq_knowledge_member", "knowledge_import_members", type_="unique"
    )
    op.create_unique_constraint(
        "uq_knowledge_member",
        "knowledge_import_members",
        ["import_id", "member_kind", "source_key_digest"],
    )
    op.create_check_constraint(
        "progress",
        "knowledge_import_members",
        "member_kind IN ('upload','workspace') AND next_chunk >= 0 AND (total_chunks IS NULL OR total_chunks >= next_chunk)",
    )


def downgrade():
    if op.get_bind().scalar(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM knowledge_imports WHERE composition_version=1)"
        )
    ):
        raise RuntimeError("Refusing to discard composed knowledge provenance")
    op.drop_constraint(
        op.f("ck_knowledge_import_members_progress"),
        "knowledge_import_members",
        type_="check",
    )
    op.drop_constraint(
        "uq_knowledge_member", "knowledge_import_members", type_="unique"
    )
    op.create_unique_constraint(
        "uq_knowledge_member",
        "knowledge_import_members",
        ["import_id", "source_key_digest"],
    )
    for name in (
        "member_kind",
        "next_chunk",
        "total_chunks",
        "declared_bytes",
        "blob_oid",
    ):
        op.drop_column("knowledge_import_members", name)
    op.drop_constraint(
        op.f("ck_knowledge_documents_accepted"), "knowledge_documents", type_="check"
    )
    op.drop_constraint(
        "fk_knowledge_document_accepted", "knowledge_documents", type_="foreignkey"
    )
    op.drop_column("knowledge_documents", "accepted_revision_id")
    op.drop_constraint(
        "uq_knowledge_revision_document", "knowledge_revisions", type_="unique"
    )
    for name in ("composition", "target_metadata", "kind"):
        op.drop_constraint(
            op.f("ck_knowledge_imports_" + name), "knowledge_imports", type_="check"
        )
    op.create_check_constraint(
        "kind", "knowledge_imports", "kind IN ('upload','workspace')"
    )
    for name in (
        "composition_version",
        "target_workspace_id",
        "target_sha",
        "target_path_scopes",
        "uploads_after",
        "uploads_done",
        "workspace_cursor",
        "workspace_done",
        "eligible_files",
        "processed_chunks",
        "scan_reason",
        "tokenizer_fingerprint",
        "chunking_version",
    ):
        op.drop_column("knowledge_imports", name)
