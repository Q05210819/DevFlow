"""Bounded source contracts shared by ingestion, indexing and citations."""

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from devflow.auth.types import Actor
from devflow.facts.schemas import SHA, StrictDTO
from devflow.jobs.schemas import JobStatus
from devflow.workspace.read import valid_path

Hash = Annotated[str, Field(strict=True, pattern=r"^[0-9a-f]{64}$")]
Count = Annotated[int, Field(strict=True, ge=0)]
Generation = Annotated[int, Field(strict=True, ge=1)]
PATH_RULE_VERSION = "knowledge-paths-v1"

AvailabilityStatus = Literal["not_indexed", "building", "ready", "stale"]
AvailabilityReason = Literal[
    "no_target",
    "target_not_indexed",
    "target_building",
    "target_failed",
    "target_pending",
    "active_unavailable",
    "vectors_reclaimed",
    "fingerprint_mismatch",
    "schema_unsupported",
    "collection_missing",
    "schema_mismatch",
    "vectors_missing",
]


@dataclass(frozen=True, slots=True)
class RepositoryEpoch:
    repository_id: UUID
    installation_id: int
    user_version: int
    installation_version: int
    repository_version: int


@dataclass(frozen=True, slots=True)
class SourceGrant:
    actor: Actor
    repository_epochs: tuple[RepositoryEpoch, ...]


MAX_QUERY_REPOSITORIES = 3
QueryTarget = Literal["target", "active"]


class IndexSelection(StrictDTO):
    """One index the user explicitly asked to search in this query."""

    repository_id: UUID
    sha: SHA
    target: QueryTarget


class QuerySelection(StrictDTO):
    """The user-owned source scope; installation grants never widen it.

    ``selected_repository_ids`` is every repository whose content may take part,
    including a fork source that owns no index. ``indexes`` only describes the
    indexes actually searched, so both sets stay distinct.
    """

    selected_repository_ids: tuple[UUID, ...] = Field(
        min_length=1, max_length=MAX_QUERY_REPOSITORIES
    )
    indexes: tuple[IndexSelection, ...] = Field(min_length=1)

    @field_validator("selected_repository_ids")
    @classmethod
    def unique_repositories(cls, values):
        if len(set(values)) != len(values):
            raise ValueError("Duplicate repository selection")
        return values

    @model_validator(mode="after")
    def bounded_indexes(self):
        owners = [item.repository_id for item in self.indexes]
        if len(set(owners)) != len(owners):
            raise ValueError("Duplicate index selection")
        if not set(owners) <= set(self.selected_repository_ids):
            raise ValueError("Every index repository must be explicitly selected")
        return self


@dataclass(frozen=True, slots=True)
class FrozenIndex:
    """One real index identity pinned when the query scope was frozen."""

    repository_id: UUID
    index_version_id: UUID
    sha: str
    index_generation: int
    target_generation: int
    member_digest: str


@dataclass(frozen=True, slots=True)
class QueryScope:
    actor: Actor
    grant: SourceGrant
    selected_repository_ids: tuple[UUID, ...]
    indexes: tuple[FrozenIndex, ...]
    # Kept so a version-change retry re-freezes the original user selection
    # rather than widening or inventing a new one.
    selection: QuerySelection | None = None


class SourceIdentity(StrictDTO):
    repository_id: UUID
    source_repository_id: UUID
    dependency_repository_ids: tuple[UUID, ...] = Field(min_length=1)
    document_id: UUID
    revision_hash: Hash
    workspace_id: UUID | None
    sha: SHA | None
    path: str | None

    @model_validator(mode="after")
    def coherent(self):
        dependencies = set(self.dependency_repository_ids)
        if (
            len(dependencies) != len(self.dependency_repository_ids)
            or not {self.repository_id, self.source_repository_id} <= dependencies
        ):
            raise ValueError("Missing or duplicate source dependencies")
        if self.workspace_id is None:
            if (
                self.sha is not None
                or self.path is not None
                or self.source_repository_id != self.repository_id
            ):
                raise ValueError("Upload identity is not a Git source")
        elif self.sha is None or not valid_path(self.path):
            raise ValueError("Workspace identity requires full SHA and path")
        return self


class ChunkLocation(StrictDTO):
    start_line: Generation
    end_line: Generation
    start_byte: Count
    end_byte: Count

    @model_validator(mode="after")
    def ordered(self):
        if self.end_line < self.start_line or self.end_byte <= self.start_byte:
            raise ValueError("Invalid original source interval")
        return self


class SourceBytes(StrictDTO):
    identity: SourceIdentity
    body: bytes = Field(strict=True, max_length=1048576)


class ChunkDraft(StrictDTO):
    text: str
    embedding_text: str
    location: ChunkLocation
    content_hash: Hash
    token_count: Annotated[int, Field(strict=True, ge=1, le=512)]
    tokenizer_fingerprint: Hash
    chunking_version: str


class SourcePreview(StrictDTO):
    identity: SourceIdentity
    text: str | None
    location: ChunkLocation | None
    is_current: bool = Field(strict=True)
    partial: bool = Field(strict=True)
    next_cursor: str | None
    reason: Literal["source_expired", "source_unavailable"] | None = None


class CoverageView(StrictDTO):
    repository_id: UUID
    sha: SHA | None
    target_generation: Generation
    included: Count
    skipped: Count
    unsupported: Count
    over_limit: Count
    unscanned: bool = Field(strict=True)
    complete: bool = Field(strict=True)


@dataclass(frozen=True, slots=True)
class Evidence:
    """One canonical chunk, already authorized and located in its source."""

    identity: SourceIdentity
    chunk_id: UUID
    index_version_id: UUID
    location: ChunkLocation
    text: str
    content_hash: str
    rank: int

    def __post_init__(self):
        if self.rank < 1 or not self.text:
            raise ValueError("Invalid evidence")


@dataclass(frozen=True, slots=True)
class EvidenceSet:
    """The final bounded answer input: evidence or an explicit empty result."""

    scope: QueryScope
    status: Literal["evidence", "no_evidence"]
    evidence: tuple[Evidence, ...]
    coverage: tuple[CoverageView, ...]

    def __post_init__(self):
        if self.status == "evidence" and not self.evidence:
            raise ValueError("Evidence status requires evidence")
        if self.status == "no_evidence" and self.evidence:
            raise ValueError("No-evidence status forbids evidence")
        if tuple(item.rank for item in self.evidence) != tuple(
            range(1, len(self.evidence) + 1)
        ):
            raise ValueError("Evidence ranks must be consecutive")


@dataclass(frozen=True, slots=True)
class CitationEntry:
    """One server-derived citation identity; never model-authored."""

    citation_id: str
    identity: SourceIdentity
    location: ChunkLocation
    url: str
    content_hash: str
    rank: int


class EvidenceManifest(StrictDTO):
    """The complete server-owned citation list for one retained result."""

    entries: tuple[CitationEntry, ...]


class EvidenceView(StrictDTO):
    """Bounded result text, addressable only through a manifest citation ID."""

    citation_id: str
    text: str
    rank: int = Field(strict=True, ge=1)


class QueryResult(StrictDTO):
    status: Literal["evidence", "no_evidence"]
    evidence: tuple[EvidenceView, ...]
    coverage: tuple[CoverageView, ...]


class QueryReceipt(StrictDTO):
    query_id: UUID
    job_id: UUID


class QueryRequest(StrictDTO):
    selection: QuerySelection
    question: str


class QueryView(StrictDTO):
    query_id: UUID
    job_id: UUID
    status: JobStatus
    selection: QuerySelection
    question: str
    manifest: EvidenceManifest | None
    result: QueryResult | None
    error_code: str | None
    created_at: datetime
    completed_at: datetime | None


class RetentionResult(StrictDTO):
    scanned: Count
    deleted: Count
    retained: Count


def upload_filename(value):
    if (
        not valid_path(value)
        or "/" in value
        or value.startswith(".")
        or not value.lower().endswith((".md", ".txt"))
    ):
        raise ValueError("Expected a single Markdown or text filename")
    return value


class UploadImportRequest(StrictDTO):
    kind: Literal["upload"]
    repository_id: UUID
    document_id: UUID | None = None
    filename: str
    staging_id: UUID

    _filename = field_validator("filename")(upload_filename)


class WorkspaceImportRequest(StrictDTO):
    kind: Literal["workspace"]
    repository_id: UUID
    workspace_id: UUID
    document_paths: tuple[str, ...] = (
        "README",
        "README.md",
        "README.txt",
        "README.rst",
        "docs",
    )
    code_paths: tuple[str, ...] = ()

    @field_validator("document_paths", "code_paths")
    @classmethod
    def paths(cls, values):
        if len(values) > 128 or any(not valid_path(path, root=True) for path in values):
            raise ValueError("Invalid path scopes")
        return tuple(sorted(set(values)))

    @model_validator(mode="after")
    def bounded(self):
        if not self.document_paths and not self.code_paths:
            raise ValueError("At least one path scope is required")
        if len(self.model_dump_json().encode("utf-8")) > 8192:
            raise ValueError("Path scope metadata exceeds budget")
        return self


class DeleteImportRequest(StrictDTO):
    kind: Literal["delete"]
    repository_id: UUID
    document_id: UUID


class ReplacementRequest(StrictDTO):
    """Upload-only replacement; the document identity comes from the route."""

    filename: str
    staging_id: UUID

    _filename = field_validator("filename")(upload_filename)


ImportRequest = Annotated[
    WorkspaceImportRequest | UploadImportRequest | DeleteImportRequest,
    Field(discriminator="kind"),
]


class ImportProgress(StrictDTO):
    import_id: UUID
    status: str
    processed: Count
    complete: bool


class IndexRequest(StrictDTO):
    repository_id: UUID
    import_id: UUID


class IndexReceipt(StrictDTO):
    index_id: UUID
    job_id: UUID
    target_generation: Generation


class ImportReceipt(StrictDTO):
    import_id: UUID
    job_id: UUID
    target_generation: Generation


def metadata_bytes(value, limit):
    """Use PostgreSQL JSONB's space-separated representation for the byte limit."""
    encoded = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
    if len(encoded) > limit:
        raise ValueError("Metadata exceeds budget")
    return value


class StageReceipt(StrictDTO):
    """A staging acknowledgement, not an import Job or an accepted revision."""

    staging_id: UUID
    expires_at: datetime


class ScanLimits(StrictDTO):
    max_files: Count
    max_scan_bytes: Count
    max_file_bytes: Count


class CorpusState(StrictDTO):
    import_id: UUID
    kind: Literal["upload", "workspace", "delete"]
    status: Literal[
        "queued", "building", "ready", "ready_partial", "failed", "superseded"
    ]
    target_generation: Generation
    included: Count
    skipped: Count
    unsupported: Count
    over_limit: Count
    unscanned: bool = Field(strict=True)
    complete: bool = Field(strict=True)
    scan_reason: str | None
    processed_chunks: Count
    created_at: datetime
    deadline: datetime


class DocumentSummary(StrictDTO):
    document_id: UUID
    kind: Literal["upload", "workspace"]
    path: str | None
    filename: str | None
    deleted: bool = Field(strict=True)
    published_revisions: Count
    latest_published_revision_id: UUID | None
    latest_published_at: datetime | None


class DocumentPage(StrictDTO):
    items: tuple[DocumentSummary, ...]
    next_cursor: str | None


class KnowledgeView(StrictDTO):
    repository_id: UUID
    target_generation: Count
    target_workspace_id: UUID | None
    target_sha: SHA | None
    path_rule_version: str
    document_paths: tuple[str, ...]
    code_paths: tuple[str, ...]
    corpus: CorpusState | None
    needs_reimport: bool = Field(strict=True)
    # Derived only from the published pointer and the current target snapshot;
    # never inferred from canonical documents alone. A storage contradiction
    # found while serving this projection is a 409 instead of a ready status.
    index_status: AvailabilityStatus
    index_reason: AvailabilityReason | None = None
    # The explicitly selectable older active version, distinct from the target
    # snapshot. Present only while that published version is still ready,
    # queryable and unpurged; it is never inferred from a status alone.
    active_index_generation: Generation | None = None
    active_index_sha: SHA | None = None
    documents: DocumentPage
    limits: ScanLimits


class RevisionSummary(StrictDTO):
    revision_id: UUID
    revision_hash: Hash
    published_at: datetime
    is_current: bool = Field(strict=True)
    # Reclaimed but published revisions keep their identity; the reason for a
    # missing body is surfaced by the preview rather than hidden from history.
    availability: Literal["available", "expired", "deleted", "unavailable"]


class DocumentView(StrictDTO):
    document_id: UUID
    repository_id: UUID
    kind: Literal["upload", "workspace"]
    path: str | None
    filename: str | None
    deleted: bool = Field(strict=True)
    revisions: tuple[RevisionSummary, ...]
    revisions_next_cursor: str | None
    preview: SourcePreview | None


class PathEntry(StrictDTO):
    path: str | None
    mode: str
    size: Count | None
    outcome: Literal["selected", "unsupported", "excluded"]
    reason: str | None


class PathPage(StrictDTO):
    items: tuple[PathEntry, ...]
    next_cursor: str | None
    exhausted: bool = Field(strict=True)
    truncated: bool = Field(strict=True)
