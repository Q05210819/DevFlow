"""Public immutable versions and the finite, server-resolved selection contract."""

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, TypeAdapter, field_validator

from devflow.facts.schemas import SHA, GitHubID, StrictDTO
from devflow.workspace.git import valid_ref


class RefSelection(StrictDTO):
    kind: Literal["branch", "tag"]
    name: str = Field(strict=True, min_length=1, max_length=1024)

    @field_validator("name")
    @classmethod
    def literal_ref(cls, value):
        if not valid_ref("refs/heads/" + value):
            raise ValueError("Invalid literal reference")
        return value


class PullSelection(StrictDTO):
    kind: Literal["pull"]
    number: GitHubID
    side: Literal["base", "head"]


class CISelection(StrictDTO):
    kind: Literal["ci"]
    run_id: GitHubID
    attempt: GitHubID


WorkspaceSelection = Annotated[
    RefSelection | PullSelection | CISelection, Field(discriminator="kind")
]
SELECTION = TypeAdapter(WorkspaceSelection)


class WorkspaceSource(StrictDTO):
    selection: WorkspaceSelection
    ref: str
    source_repository_id: GitHubID
    transfer_repository_id: GitHubID
    base_sha: SHA | None = None
    head_sha: SHA | None = None


class WorkspaceView(StrictDTO):
    id: UUID
    repository_id: UUID
    source_repository_id: UUID
    commit_sha: SHA
    tree_oid: SHA
    source: WorkspaceSource
    status: Literal["queued", "running", "retry_wait", "ready", "failed", "unavailable"]
    error: str | None = None
    created_at: datetime
    last_used_at: datetime


class WorkspaceResult(StrictDTO):
    workspace_id: UUID
    commit_sha: SHA


class WorkspacePage(StrictDTO):
    items: list[WorkspaceView]
    next_cursor: str | None = None
    has_more: bool = False
    unavailable_count: int = Field(ge=0)


ReadReason = Literal[
    "unsupported_path",
    "too_large",
    "binary",
    "non_utf8",
    "lfs_pointer",
    "symlink",
    "gitlink",
    "tree_limit",
    "response_limit",
    "line_limit",
    "file_limit",
    "byte_limit",
    "match_limit",
]


class ReadScope(StrictDTO):
    repository_id: UUID
    workspace_id: UUID
    source_repository_id: UUID
    commit_sha: SHA
    path: str


class CodeCitation(ReadScope):
    blob_oid: SHA
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    source_url: str


class TreeItem(StrictDTO):
    path: str | None
    display_name: str
    kind: Literal["directory", "file", "symlink", "gitlink", "unsupported"]
    mode: str
    oid: SHA | None
    reason: ReadReason | None = None
    source_url: str | None = None


class TreePage(ReadScope):
    items: list[TreeItem]
    next_cursor: str | None = None
    has_more: bool = False
    complete: bool
    truncated: bool
    scanned: int = Field(ge=0)
    skipped: dict[str, int]
    reason: ReadReason | None = None


class FileView(ReadScope):
    kind: Literal[
        "text",
        "too_large",
        "binary",
        "non_utf8",
        "lfs_pointer",
        "symlink",
        "gitlink",
        "unavailable",
    ]
    blob_oid: SHA | None
    text: str = ""
    size_bytes: int | None = None
    requested_start_line: int
    requested_line_count: int
    start_line: int | None = None
    end_line: int | None = None
    total_lines: int | None = None
    next_line: int | None = None
    source_url: str | None = None
    complete: bool = False
    truncated: bool = False
    scanned: int = Field(ge=0)
    skipped: dict[str, int]
    reason: ReadReason | None = None


class SearchScan(StrictDTO):
    tree_entries: int = Field(ge=0)
    candidates: int = Field(ge=0)
    text_files: int = Field(ge=0)
    blob_bytes: int = Field(ge=0)


class SearchHit(StrictDTO):
    citation: CodeCitation
    text: str | None
    reason: Literal["response_limit"] | None = None


class SearchView(ReadScope):
    query: str
    items: list[SearchHit]
    complete: bool
    truncated: bool
    scanned: SearchScan
    skipped: dict[str, int]
    reason: ReadReason | None = None
    snippets_truncated: bool = False
