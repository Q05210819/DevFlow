"""Canonical bodies, immutable provenance and bounded ingestion bookkeeping."""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Computed,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from devflow.db import Base


class KnowledgeCorpus(Base):
    __tablename__ = "knowledge_corpora"
    __table_args__ = (
        CheckConstraint("target_generation >= 0", name="generation"),
        CheckConstraint(
            "target_sha IS NULL OR target_sha ~ '^[0-9a-f]{40}$'", name="sha"
        ),
        CheckConstraint("octet_length(path_scopes::text) <= 8192", name="metadata"),
    )
    repository_id: Mapped[UUID] = mapped_column(
        ForeignKey("repositories.id"), primary_key=True
    )
    target_generation: Mapped[int] = mapped_column(BigInteger, server_default="0")
    target_workspace_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("workspace_versions.id")
    )
    target_sha: Mapped[str | None] = mapped_column(String(40))
    path_rule_version: Mapped[str] = mapped_column(
        String(64), server_default="knowledge-paths-v1"
    )
    path_scopes: Mapped[dict] = mapped_column(JSONB, server_default="{}")
    active_index_version_id: Mapped[UUID | None] = mapped_column(
        ForeignKey(
            "knowledge_index_versions.id",
            name="fk_knowledge_corpora_active_index_version",
            use_alter=True,
        )
    )


class IndexVersion(Base):
    """One immutable candidate build of a corpus generation.

    Vectors live in Milvus under the version id; PostgreSQL owns the lifecycle
    and the atomic ``active_index_version_id`` pointer. Nothing in this table
    can point at a newer corpus target than the one it was requested for.
    """

    __tablename__ = "knowledge_index_versions"
    __table_args__ = (
        UniqueConstraint(
            "repository_id", "idempotency_key", name="uq_knowledge_index_request"
        ),
        UniqueConstraint("job_id", name="uq_knowledge_index_job"),
        CheckConstraint(
            "target_generation >= 1"
            " AND (target_sha IS NULL OR target_sha ~ '^[0-9a-f]{40}$')",
            name="versions",
        ),
        CheckConstraint(
            "status IN ('queued','building','ready','retired','failed','superseded')",
            name="status",
        ),
        CheckConstraint(
            "expected_member_count >= 0"
            " AND written_count BETWEEN 0 AND expected_member_count",
            name="members",
        ),
        CheckConstraint("deadline = created_at + interval '24 hours'", name="deadline"),
        CheckConstraint("failures BETWEEN 0 AND 3", name="failures"),
        CheckConstraint("member_digest ~ '^[0-9a-f]{64}$'", name="digest"),
        CheckConstraint(
            "idempotency_key ~ '^[!-~]{1,128}$' AND request_hash ~ '^[0-9a-f]{64}$'",
            name="request",
        ),
        CheckConstraint(
            "octet_length(authorization_scope::text) <= 16384"
            " AND octet_length(path_scopes::text) <= 8192"
            " AND octet_length(fingerprint::text) <= 4096",
            name="metadata",
        ),
        CheckConstraint(
            "status NOT IN ('ready','retired') OR published_at IS NOT NULL",
            name="published",
        ),
        CheckConstraint("NOT queryable OR status = 'ready'", name="queryable"),
        Index(
            "ix_knowledge_index_versions_repository_status",
            "repository_id",
            "status",
            "created_at",
        ),
        Index("ix_knowledge_index_versions_purged", "purged_at", "created_at"),
        # A repository can own at most one candidate in progress. A newer
        # request or import supersedes the older candidate in the same
        # transaction before inserting, so this is a real invariant rather
        # than a serialization side effect.
        Index(
            "uq_knowledge_index_versions_one_unfinished",
            "repository_id",
            unique=True,
            postgresql_where=text("status IN ('queued', 'building')"),
        ),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    repository_id: Mapped[UUID] = mapped_column(ForeignKey("repositories.id"))
    import_id: Mapped[UUID] = mapped_column(ForeignKey("knowledge_imports.id"))
    job_id: Mapped[UUID] = mapped_column(ForeignKey("jobs.id"))
    requested_by: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    target_generation: Mapped[int] = mapped_column(BigInteger)
    # Upload-only generations have no Git SHA; that is a real snapshot, not a
    # missing one, and the manifest digest still pins the indexed content.
    target_sha: Mapped[str | None] = mapped_column(String(40))
    path_rule_version: Mapped[str] = mapped_column(
        String(64), server_default="knowledge-paths-v1"
    )
    path_scopes: Mapped[dict] = mapped_column(JSONB, server_default="{}")
    idempotency_key: Mapped[str] = mapped_column(String(128))
    request_hash: Mapped[str] = mapped_column(String(64))
    # ``authorization`` is a reserved SQL word; the stored authority snapshot
    # keeps its ORM meaning while the physical column stays unquoted-safe.
    authorization: Mapped[dict] = mapped_column("authorization_scope", JSONB)
    status: Mapped[str] = mapped_column(String(16), server_default="queued")
    fingerprint: Mapped[dict] = mapped_column(JSONB, server_default="{}")
    expected_member_count: Mapped[int] = mapped_column(Integer)
    written_count: Mapped[int] = mapped_column(Integer, server_default="0")
    member_digest: Mapped[str] = mapped_column(String(64))
    queryable: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.clock_timestamp()
    )
    deadline: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    failures: Mapped[int] = mapped_column(Integer, server_default="0")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.clock_timestamp(),
        onupdate=func.clock_timestamp(),
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    purged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class IndexMember(Base):
    """The exact chunk manifest a version promises to contain.

    ``chunk_id`` deliberately has no foreign key: vector retention outlives a
    reclaimed revision body, and the manifest is the proof of what was written.
    """

    __tablename__ = "knowledge_index_members"
    __table_args__ = (
        UniqueConstraint(
            "index_version_id", "chunk_id", name="uq_knowledge_index_member"
        ),
        UniqueConstraint(
            "index_version_id", "ordinal", name="uq_knowledge_index_member_ordinal"
        ),
        CheckConstraint("ordinal >= 0", name="ordinal"),
        CheckConstraint("status IN ('pending','written')", name="status"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    index_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("knowledge_index_versions.id", ondelete="CASCADE")
    )
    chunk_id: Mapped[UUID] = mapped_column()
    ordinal: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), server_default="pending")
    written_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class KnowledgeImport(Base):
    __tablename__ = "knowledge_imports"
    __table_args__ = (
        UniqueConstraint(
            "actor_id", "repository_id", "idempotency_key", name="uq_knowledge_request"
        ),
        UniqueConstraint(
            "repository_id", "target_generation", name="uq_knowledge_generation"
        ),
        CheckConstraint(
            "target_generation >= 1 AND actor_auth_version >= 1 AND actor_github_id > 0",
            name="versions",
        ),
        CheckConstraint("kind IN ('upload','workspace','delete')", name="kind"),
        CheckConstraint(
            "status IN ('queued','building','ready','ready_partial','failed','superseded')",
            name="status",
        ),
        CheckConstraint(
            "request_hash ~ '^[0-9a-f]{64}$' AND idempotency_key ~ '^[!-~]{1,128}$'",
            name="request",
        ),
        CheckConstraint("octet_length(request::text) <= 8192", name="metadata"),
        CheckConstraint("deadline = created_at + interval '24 hours'", name="deadline"),
        CheckConstraint(
            "composition_version IN (0,1) AND workspace_cursor >= 0 AND eligible_files BETWEEN 0 AND 2000 AND processed_chunks >= 0",
            name="composition",
        ),
        CheckConstraint(
            "octet_length(target_path_scopes::text) <= 8192", name="target_metadata"
        ),
        CheckConstraint(
            "cursor >= 0 AND included >= 0 AND skipped >= 0 AND unsupported >= 0 AND over_limit >= 0 AND scanned_bytes BETWEEN 0 AND 33554432 AND failures BETWEEN 0 AND 3",
            name="progress",
        ),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    repository_id: Mapped[UUID] = mapped_column(
        ForeignKey("knowledge_corpora.repository_id")
    )
    source_repository_id: Mapped[UUID] = mapped_column(ForeignKey("repositories.id"))
    actor_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    actor_github_id: Mapped[int] = mapped_column(BigInteger)
    actor_auth_version: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(16))
    idempotency_key: Mapped[str] = mapped_column(String(128))
    request_hash: Mapped[str] = mapped_column(String(64))
    request: Mapped[dict] = mapped_column(JSONB)
    target_generation: Mapped[int] = mapped_column(BigInteger)
    workspace_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("workspace_versions.id")
    )
    sha: Mapped[str | None] = mapped_column(String(40))
    path_rule_version: Mapped[str] = mapped_column(
        String(64), server_default="knowledge-paths-v1"
    )
    job_id: Mapped[UUID] = mapped_column(ForeignKey("jobs.id"), unique=True)
    status: Mapped[str] = mapped_column(String(16), server_default="queued")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    deadline: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    cursor: Mapped[int] = mapped_column(BigInteger, server_default="0")
    included: Mapped[int] = mapped_column(Integer, server_default="0")
    skipped: Mapped[int] = mapped_column(Integer, server_default="0")
    unsupported: Mapped[int] = mapped_column(Integer, server_default="0")
    over_limit: Mapped[int] = mapped_column(Integer, server_default="0")
    scanned_bytes: Mapped[int] = mapped_column(BigInteger, server_default="0")
    failures: Mapped[int] = mapped_column(Integer, server_default="0")
    unscanned: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    complete: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    composition_version: Mapped[int] = mapped_column(Integer, server_default="0")
    target_workspace_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("workspace_versions.id")
    )
    target_sha: Mapped[str | None] = mapped_column(String(40))
    target_path_scopes: Mapped[dict] = mapped_column(JSONB, server_default="{}")
    uploads_after: Mapped[UUID | None]
    uploads_done: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    workspace_cursor: Mapped[int] = mapped_column(Integer, server_default="0")
    workspace_done: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    eligible_files: Mapped[int] = mapped_column(Integer, server_default="0")
    processed_chunks: Mapped[int] = mapped_column(Integer, server_default="0")
    scan_reason: Mapped[str | None] = mapped_column(String(64))
    tokenizer_fingerprint: Mapped[str | None] = mapped_column(String(64))
    chunking_version: Mapped[str | None] = mapped_column(String(128))


class ImportRepositoryEpoch(Base):
    __tablename__ = "knowledge_import_epochs"
    __table_args__ = (
        CheckConstraint(
            "user_version >= 1 AND installation_version >= 1 AND repository_version >= 1",
            name="versions",
        ),
    )
    import_id: Mapped[UUID] = mapped_column(
        ForeignKey("knowledge_imports.id"), primary_key=True
    )
    repository_id: Mapped[UUID] = mapped_column(
        ForeignKey("repositories.id"), primary_key=True
    )
    installation_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("github_installations.installation_id")
    )
    user_version: Mapped[int] = mapped_column(Integer)
    installation_version: Mapped[int] = mapped_column(Integer)
    repository_version: Mapped[int] = mapped_column(Integer)


class Document(Base):
    __tablename__ = "knowledge_documents"
    __table_args__ = (
        ForeignKeyConstraint(
            ["id", "accepted_revision_id"],
            ["knowledge_revisions.document_id", "knowledge_revisions.id"],
            name="fk_knowledge_document_accepted",
            use_alter=True,
        ),
        CheckConstraint(
            "accepted_revision_id IS NULL OR (kind='upload' AND NOT deleted)",
            name="accepted",
        ),
        CheckConstraint("kind IN ('upload','workspace')", name="kind"),
        CheckConstraint("octet_length(attributes::text) <= 8192", name="metadata"),
        CheckConstraint(
            "(kind='upload' AND path IS NULL AND repository_id=source_repository_id) OR (kind='workspace' AND path IS NOT NULL AND octet_length(path) BETWEEN 1 AND 4096)",
            name="source",
        ),
        Index(
            "uq_knowledge_workspace_document",
            "repository_id",
            "source_repository_id",
            "path_digest",
            unique=True,
            postgresql_where=text("kind='workspace'"),
        ),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    repository_id: Mapped[UUID] = mapped_column(ForeignKey("repositories.id"))
    source_repository_id: Mapped[UUID] = mapped_column(ForeignKey("repositories.id"))
    kind: Mapped[str] = mapped_column(String(16))
    path: Mapped[str | None] = mapped_column(String(4096))
    # Bounded index only, never source identity. A digest collision rejects the
    # write; consumers must compare the full exact path before reusing any row.
    path_digest: Mapped[str | None] = mapped_column(
        String(32), Computed("md5(path::text)", persisted=True)
    )
    attributes: Mapped[dict] = mapped_column(JSONB, server_default="{}")
    deleted: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    accepted_revision_id: Mapped[UUID | None]


class DocumentRevision(Base):
    __tablename__ = "knowledge_revisions"
    __table_args__ = (
        UniqueConstraint("document_id", "id", name="uq_knowledge_revision_document"),
        UniqueConstraint(
            "document_id",
            "source_version",
            "revision_hash",
            name="uq_knowledge_revision",
        ),
        CheckConstraint(
            "revision_hash ~ '^[0-9a-f]{64}$' AND source_version ~ '^[0-9a-f]{64}$'",
            name="hashes",
        ),
        CheckConstraint(
            "(workspace_id IS NULL AND sha IS NULL) OR (workspace_id IS NOT NULL AND sha ~ '^[0-9a-f]{40}$')",
            name="source",
        ),
        CheckConstraint(
            "body_bytes BETWEEN 0 AND 1048576 AND (body IS NULL OR octet_length(body)=body_bytes)",
            name="body",
        ),
        CheckConstraint(
            "availability IN ('available','deleted','expired','unavailable') AND (NOT searchable OR (availability='available' AND body IS NOT NULL AND published_at IS NOT NULL))",
            name="availability",
        ),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    document_id: Mapped[UUID] = mapped_column(ForeignKey("knowledge_documents.id"))
    revision_hash: Mapped[str] = mapped_column(String(64))
    source_version: Mapped[str] = mapped_column(String(64))
    workspace_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("workspace_versions.id")
    )
    sha: Mapped[str | None] = mapped_column(String(40))
    body: Mapped[bytes | None] = mapped_column(LargeBinary)
    body_bytes: Mapped[int] = mapped_column(Integer)
    searchable: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    availability: Mapped[str] = mapped_column(String(16), server_default="available")
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.clock_timestamp()
    )


class RevisionDependency(Base):
    __tablename__ = "knowledge_revision_dependencies"
    revision_id: Mapped[UUID] = mapped_column(
        ForeignKey("knowledge_revisions.id"), primary_key=True
    )
    repository_id: Mapped[UUID] = mapped_column(
        ForeignKey("repositories.id"), primary_key=True
    )


class ImportMember(Base):
    __tablename__ = "knowledge_import_members"
    __table_args__ = (
        UniqueConstraint(
            "import_id", "member_kind", "source_key_digest", name="uq_knowledge_member"
        ),
        UniqueConstraint("import_id", "ordinal", name="uq_knowledge_member_ordinal"),
        CheckConstraint(
            "ordinal >= 0 AND octet_length(source_key) BETWEEN 1 AND 4096",
            name="identity",
        ),
        CheckConstraint(
            "status IN ('pending','included','skipped','unsupported','over_limit')",
            name="status",
        ),
        CheckConstraint(
            "member_kind IN ('upload','workspace') AND next_chunk >= 0 AND (total_chunks IS NULL OR total_chunks >= next_chunk)",
            name="progress",
        ),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    import_id: Mapped[UUID] = mapped_column(ForeignKey("knowledge_imports.id"))
    source_key: Mapped[str] = mapped_column(String(4096))
    # Same fail-closed collision policy as Document.path_digest; keep source_key.
    source_key_digest: Mapped[str] = mapped_column(
        String(32), Computed("md5(source_key::text)", persisted=True)
    )
    ordinal: Mapped[int] = mapped_column(Integer)
    revision_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("knowledge_revisions.id")
    )
    status: Mapped[str] = mapped_column(String(16), server_default="pending")
    reason: Mapped[str | None] = mapped_column(String(64))
    member_kind: Mapped[str] = mapped_column(String(16), server_default="upload")
    next_chunk: Mapped[int] = mapped_column(Integer, server_default="0")
    total_chunks: Mapped[int | None] = mapped_column(Integer)
    declared_bytes: Mapped[int | None] = mapped_column(Integer)
    blob_oid: Mapped[str | None] = mapped_column(String(40))


class Chunk(Base):
    __tablename__ = "knowledge_chunks"
    __table_args__ = (
        UniqueConstraint(
            "revision_id",
            "tokenizer_fingerprint",
            "chunking_version",
            "ordinal",
            name="uq_knowledge_chunk",
        ),
        CheckConstraint(
            "ordinal >= 0 AND token_count BETWEEN 1 AND 512", name="tokens"
        ),
        CheckConstraint(
            "octet_length(embedding_text) <= 8192 AND octet_length(text) <= 8192 AND octet_length(attributes::text) <= 2048",
            name="bytes",
        ),
        CheckConstraint(
            "start_line >= 1 AND end_line >= start_line AND start_byte >= 0 AND end_byte > start_byte",
            name="location",
        ),
        CheckConstraint(
            "content_hash ~ '^[0-9a-f]{64}$' AND tokenizer_fingerprint ~ '^[0-9a-f]{64}$'",
            name="hashes",
        ),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    revision_id: Mapped[UUID] = mapped_column(ForeignKey("knowledge_revisions.id"))
    ordinal: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    embedding_text: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64))
    token_count: Mapped[int] = mapped_column(Integer)
    tokenizer_fingerprint: Mapped[str] = mapped_column(String(64))
    chunking_version: Mapped[str] = mapped_column(String(128))
    start_line: Mapped[int] = mapped_column(Integer)
    end_line: Mapped[int] = mapped_column(Integer)
    start_byte: Mapped[int] = mapped_column(Integer)
    end_byte: Mapped[int] = mapped_column(Integer)
    attributes: Mapped[dict] = mapped_column(JSONB, server_default="{}")


class RevisionRetention(Base):
    __tablename__ = "knowledge_revision_retentions"
    __table_args__ = (
        CheckConstraint("owner_kind IN ('query','message')", name="owner_kind"),
    )
    owner_kind: Mapped[str] = mapped_column(String(16), primary_key=True)
    owner_id: Mapped[UUID] = mapped_column(primary_key=True)
    revision_id: Mapped[UUID] = mapped_column(
        ForeignKey("knowledge_revisions.id"), primary_key=True
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class KnowledgeQuery(Base):
    """Immutable question/selection plus its bounded retained result payload."""

    __tablename__ = "knowledge_queries"
    __table_args__ = (
        UniqueConstraint("job_id", name="uq_knowledge_queries_job"),
        Index("ix_knowledge_queries_actor_created", "actor_id", "created_at"),
        CheckConstraint(
            "question_bytes = octet_length(question)"
            " AND question_bytes BETWEEN 1 AND 2048",
            name="question",
        ),
        CheckConstraint("octet_length(selection::text) <= 16384", name="selection"),
        CheckConstraint(
            "manifest IS NULL OR octet_length(manifest::text) <= 65536",
            name="manifest",
        ),
        CheckConstraint(
            "result IS NULL OR octet_length(result::text) <= 131072",
            name="result",
        ),
        CheckConstraint("(manifest IS NULL) = (result IS NULL)", name="payload"),
        CheckConstraint(
            "retention_expires_at = created_at + interval '1 hour'",
            name="retention",
        ),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    job_id: Mapped[UUID] = mapped_column(ForeignKey("jobs.id"))
    actor_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    question: Mapped[str] = mapped_column(Text)
    question_bytes: Mapped[int] = mapped_column(Integer)
    selection: Mapped[dict] = mapped_column(JSONB)
    manifest: Mapped[dict | None] = mapped_column(JSONB)
    result: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.clock_timestamp()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retention_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    reclaimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class KnowledgeQueryRepository(Base):
    """The bounded explicit scope, ordered for stable private Job visibility."""

    __tablename__ = "knowledge_query_repositories"
    __table_args__ = (
        UniqueConstraint(
            "query_id", "ordinal", name="uq_knowledge_query_repository_ordinal"
        ),
        CheckConstraint("ordinal BETWEEN 0 AND 2", name="ordinal"),
    )
    query_id: Mapped[UUID] = mapped_column(
        ForeignKey("knowledge_queries.id", ondelete="CASCADE"), primary_key=True
    )
    repository_id: Mapped[UUID] = mapped_column(
        ForeignKey("repositories.id"), primary_key=True
    )
    ordinal: Mapped[int] = mapped_column(Integer)


class RevisionPin(Base):
    __tablename__ = "knowledge_revision_pins"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    revision_id: Mapped[UUID] = mapped_column(
        ForeignKey("knowledge_revisions.id"), index=True
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class UploadStage(Base):
    __tablename__ = "knowledge_upload_stages"
    __table_args__ = (
        CheckConstraint(
            "purpose IS NULL OR purpose IN ('new','replace')", name="purpose"
        ),
        CheckConstraint(
            "(idempotency_key IS NULL AND request_hash IS NULL AND purpose IS NULL"
            " AND repository_id IS NULL AND body_bytes IS NULL)"
            " OR (idempotency_key IS NOT NULL AND request_hash ~ '^[0-9a-f]{64}$'"
            " AND purpose IS NOT NULL AND repository_id IS NOT NULL"
            " AND body_bytes BETWEEN 0 AND 1048576"
            " AND (purpose='new' OR document_id IS NOT NULL))",
            name="keyed",
        ),
        Index(
            "uq_knowledge_stage_request",
            "actor_id",
            "repository_id",
            "idempotency_key",
            unique=True,
            postgresql_where=text("idempotency_key IS NOT NULL"),
        ),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    actor_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    revision_id: Mapped[UUID] = mapped_column(ForeignKey("knowledge_revisions.id"))
    filename: Mapped[str] = mapped_column(String(4096))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    consumed_import_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("knowledge_imports.id"), unique=True
    )
    # Keyed HTTP staging only; historical and internal unkeyed rows keep NULL.
    repository_id: Mapped[UUID | None] = mapped_column(ForeignKey("repositories.id"))
    purpose: Mapped[str | None] = mapped_column(String(16))
    idempotency_key: Mapped[str | None] = mapped_column(String(128))
    request_hash: Mapped[str | None] = mapped_column(String(64))
    document_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("knowledge_documents.id")
    )
    body_bytes: Mapped[int | None] = mapped_column(Integer)


class KnowledgeCapacity(Base):
    __tablename__ = "knowledge_capacity"
    __table_args__ = (
        CheckConstraint("id = 1", name="singleton"),
        CheckConstraint(
            "body_bytes >= 0 AND reserved_bytes >= 0 AND body_bytes + reserved_bytes <= 134217728",
            name="bytes",
        ),
        CheckConstraint(
            "chunks >= 0 AND reserved_chunks >= 0 AND chunks + reserved_chunks <= 40000",
            name="chunks",
        ),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    body_bytes: Mapped[int] = mapped_column(BigInteger, server_default="0")
    reserved_bytes: Mapped[int] = mapped_column(BigInteger, server_default="0")
    chunks: Mapped[int] = mapped_column(Integer, server_default="0")
    reserved_chunks: Mapped[int] = mapped_column(Integer, server_default="0")


class RepositoryCapacity(Base):
    __tablename__ = "knowledge_repository_capacity"
    __table_args__ = (
        CheckConstraint(
            "chunks >= 0 AND reserved_chunks >= 0 AND chunks + reserved_chunks <= 10000",
            name="chunks",
        ),
    )
    repository_id: Mapped[UUID] = mapped_column(
        ForeignKey("repositories.id"), primary_key=True
    )
    chunks: Mapped[int] = mapped_column(Integer, server_default="0")
    reserved_chunks: Mapped[int] = mapped_column(Integer, server_default="0")
