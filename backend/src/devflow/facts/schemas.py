"""Persistence whitelist. Upstream dictionaries must be normalized into these DTOs."""

import hashlib
from datetime import UTC
from typing import Annotated, Literal
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    field_validator,
    model_validator,
)

GitHubID = Annotated[int, Field(strict=True, gt=0)]
SHA = Annotated[str, Field(strict=True, pattern=r"^[0-9a-f]{40}$")]
UTCDateTime = Annotated[
    AwareDatetime, AfterValidator(lambda value: value.astimezone(UTC))
]
FactKind = Literal[
    "issue",
    "pull",
    "comment",
    "review",
    "review_comment",
    "pr_file",
    "ci_run",
    "ci_job",
]
ParentKind = Literal["issue", "pull", "ci_run"]
CollectionKind = Literal[
    "comments", "reviews", "review_comments", "files", "attempts", "jobs"
]
CollectionReason = Literal[
    "permission_missing",
    "github_unavailable",
    "github_rate_limited",
    "github_response_too_large",
    "source_limit",
    "budget_exhausted",
    "version_changed",
    "version_unavailable",
    "partial",
    "not_collected",
]
CIStatus = Literal[
    "queued", "in_progress", "completed", "waiting", "requested", "pending", "unknown"
]
CIConclusion = Literal[
    "success",
    "failure",
    "neutral",
    "cancelled",
    "skipped",
    "timed_out",
    "action_required",
    "stale",
    "startup_failure",
    "unknown",
]


def _source_url(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or parsed.hostname not in {"github.com", "api.github.com"}
        or parsed.username
        or parsed.password
        or parsed.port not in {None, 443}
    ):
        raise ValueError("Source URL must be a GitHub HTTPS URL")
    return value


SourceURL = Annotated[
    str, Field(strict=True, max_length=4096), AfterValidator(_source_url)
]


class StrictDTO(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _known_or_unknown(value, choices):
    # Only future strings are unknown states; malformed objects still fail DTO validation.
    if not isinstance(value, str):
        return value
    return value if value in choices else "unknown"


class GitHubUser(StrictDTO):
    id: GitHubID
    login: str | None = None


class NormalizedPayload(StrictDTO):
    normalization_reasons: list[
        Literal["unknown_value", "content_truncated", "field_unavailable"]
    ] = Field(default_factory=list, max_length=3)


class IssuePayload(NormalizedPayload):
    title: str
    body: str | None = None
    state: Literal["open", "closed", "unknown"]
    labels: list[str] = Field(default_factory=list)
    assignees: list[GitHubUser] = Field(default_factory=list)
    author: GitHubUser | None = None
    created_at: UTCDateTime | None = None
    closed_at: UTCDateTime | None = None

    @field_validator("state", mode="before")
    @classmethod
    def unknown_state(cls, value):
        return _known_or_unknown(value, {"open", "closed"})


class PullPayload(IssuePayload):
    issue_id: GitHubID
    base_repository_id: GitHubID | None = None
    head_repository_id: GitHubID | None = None
    base_ref: str | None = None
    head_ref: str | None = None
    base_sha: SHA
    head_sha: SHA
    draft: bool = Field(strict=True)
    merged: bool = Field(strict=True)
    merge_commit_sha: SHA | None = None
    merged_at: UTCDateTime | None = None


class CommentPayload(NormalizedPayload):
    body: str | None = None
    author: GitHubUser | None = None
    created_at: UTCDateTime | None = None


class ReviewPayload(CommentPayload):
    state: Literal[
        "pending", "commented", "approved", "changes_requested", "dismissed", "unknown"
    ]
    commit_id: SHA | None = None
    submitted_at: UTCDateTime | None = None

    @field_validator("state", mode="before")
    @classmethod
    def normalized_state(cls, value):
        value = value.lower() if isinstance(value, str) else value
        return _known_or_unknown(
            value,
            {"pending", "commented", "approved", "changes_requested", "dismissed"},
        )


class ReviewCommentPayload(CommentPayload):
    review_id: GitHubID | None = None
    commit_id: SHA | None = None
    original_commit_id: SHA | None = None
    path: str | None = None
    side: Literal["LEFT", "RIGHT", "unknown"] | None = None
    line: GitHubID | None = None
    original_line: GitHubID | None = None
    start_line: GitHubID | None = None
    original_start_line: GitHubID | None = None
    in_reply_to_id: GitHubID | None = None

    @field_validator("side", mode="before")
    @classmethod
    def normalized_side(cls, value):
        return _known_or_unknown(value, {"LEFT", "RIGHT"})


class PRFilePayload(NormalizedPayload):
    base_sha: SHA
    head_sha: SHA
    filename: str = Field(min_length=1)
    previous_filename: str | None = None
    sha: SHA | None = None
    status: Literal[
        "added",
        "removed",
        "modified",
        "renamed",
        "copied",
        "changed",
        "unchanged",
        "unknown",
    ]
    additions: int = Field(strict=True, ge=0)
    deletions: int = Field(strict=True, ge=0)
    changes: int = Field(strict=True, ge=0)
    patch: str | None = None
    patch_status: Literal["available", "missing", "truncated"]

    @field_validator("status", mode="before")
    @classmethod
    def normalized_status(cls, value):
        return _known_or_unknown(
            value,
            {
                "added",
                "removed",
                "modified",
                "renamed",
                "copied",
                "changed",
                "unchanged",
            },
        )

    @model_validator(mode="after")
    def consistent_patch(self):
        if (self.patch_status == "missing") != (self.patch is None):
            raise ValueError("Patch availability must match stored patch")
        return self


class CIPayload(NormalizedPayload):
    status: CIStatus
    conclusion: CIConclusion | None = None

    @field_validator("status", mode="before")
    @classmethod
    def normalized_status(cls, value):
        return _known_or_unknown(
            value,
            {"queued", "in_progress", "completed", "waiting", "requested", "pending"},
        )

    @field_validator("conclusion", mode="before")
    @classmethod
    def normalized_conclusion(cls, value):
        return _known_or_unknown(
            value,
            {
                "success",
                "failure",
                "neutral",
                "cancelled",
                "skipped",
                "timed_out",
                "action_required",
                "stale",
                "startup_failure",
            },
        )


class CIRunPayload(CIPayload):
    attempt: GitHubID
    workflow_id: GitHubID
    workflow_path: str | None = None
    name: str | None = None
    event: str
    branch: str | None = None
    head_sha: SHA
    created_at: UTCDateTime
    updated_at: UTCDateTime
    run_started_at: UTCDateTime | None = None


class CIStepPayload(CIPayload):
    number: GitHubID
    name: str
    started_at: UTCDateTime | None = None
    completed_at: UTCDateTime | None = None


class CIJobPayload(CIPayload):
    run_id: GitHubID
    attempt: GitHubID
    head_sha: SHA
    name: str
    started_at: UTCDateTime | None = None
    completed_at: UTCDateTime | None = None
    steps: list[CIStepPayload] = Field(default_factory=list)


FactPayload = (
    IssuePayload
    | PullPayload
    | CommentPayload
    | ReviewPayload
    | ReviewCommentPayload
    | PRFilePayload
    | CIRunPayload
    | CIJobPayload
)
_PAYLOADS = {
    "issue": IssuePayload,
    "pull": PullPayload,
    "comment": CommentPayload,
    "review": ReviewPayload,
    "review_comment": ReviewCommentPayload,
    "pr_file": PRFilePayload,
    "ci_run": CIRunPayload,
    "ci_job": CIJobPayload,
}
_SHA = TypeAdapter(SHA)
_ID = TypeAdapter(GitHubID)


def validate_fact_payload(kind: FactKind, payload: FactPayload | dict) -> FactPayload:
    if isinstance(payload, BaseModel):
        payload = payload.model_dump()
    return _PAYLOADS[kind].model_validate(payload)


def pr_file_version_key(*, base_sha: str, head_sha: str, filename: str) -> str:
    base, head = _SHA.validate_python(base_sha), _SHA.validate_python(head_sha)
    filename = TypeAdapter(
        Annotated[str, Field(strict=True, min_length=1)]
    ).validate_python(filename)
    return f"{base}:{head}:{hashlib.sha256(filename.encode('utf-8')).hexdigest()}"


def ci_run_version_key(*, attempt: int) -> str:
    return str(_ID.validate_python(attempt))


class FactInput(StrictDTO):
    repository_id: UUID
    kind: FactKind
    source_id: GitHubID
    version_key: str = Field(default="", max_length=256)
    number: GitHubID | None = None
    parent_kind: ParentKind | None = None
    parent_source_id: GitHubID | None = None
    state: str | None = Field(default=None, max_length=32)
    source_updated_at: UTCDateTime | None = None
    observed_at: UTCDateTime
    source_url: SourceURL
    unavailable: bool = Field(default=False, strict=True)
    payload: FactPayload

    @field_validator("payload", mode="before")
    @classmethod
    def typed_payload(cls, value, info):
        kind = info.data.get("kind")
        if kind is None:
            raise ValueError("Fact kind is required before payload")
        return validate_fact_payload(kind, value)

    @model_validator(mode="after")
    def canonical_identity(self):
        if (self.parent_kind is None) != (self.parent_source_id is None):
            raise ValueError("Parent kind and source ID must be paired")
        allowed_parents = {
            "comment": {"issue", "pull"},
            "review": {"pull"},
            "review_comment": {"pull"},
            "pr_file": {"pull"},
            "ci_job": {"ci_run"},
        }
        if (
            self.kind in allowed_parents
            and self.parent_kind not in allowed_parents[self.kind]
        ):
            raise ValueError("Fact requires its canonical parent identity")
        if self.kind not in allowed_parents and self.parent_kind is not None:
            raise ValueError("Top-level fact cannot carry a parent")
        expected_version = ""
        if self.kind == "pr_file":
            if self.source_id != self.parent_source_id:
                raise ValueError("File source ID must be its canonical PR ID")
            expected_version = pr_file_version_key(
                base_sha=self.payload.base_sha,
                head_sha=self.payload.head_sha,
                filename=self.payload.filename,
            )
        elif self.kind == "ci_run":
            expected_version = ci_run_version_key(attempt=self.payload.attempt)
        elif self.kind == "ci_job" and self.parent_source_id != self.payload.run_id:
            raise ValueError("Job parent must match its run ID")
        if self.version_key != expected_version:
            raise ValueError("Fact version key does not match its source version")
        if self.kind in {"issue", "pull"} and self.number is None:
            raise ValueError("Issue/PR number is required")
        return self


class CollectionInput(StrictDTO):
    repository_id: UUID
    parent_kind: ParentKind
    parent_source_id: GitHubID
    collection: CollectionKind
    version_key: str = Field(default="", max_length=256)
    complete: bool = Field(strict=True)
    reason: CollectionReason | None = None
    observed_at: UTCDateTime

    @model_validator(mode="after")
    def consistent_collection(self):
        if self.complete and self.reason is not None:
            raise ValueError("Complete collection cannot have a gap reason")
        if not self.complete and self.reason is None:
            raise ValueError("Partial collection requires a gap reason")
        allowed = {
            "issue": {"comments"},
            "pull": {"comments", "reviews", "review_comments", "files"},
            "ci_run": {"attempts", "jobs"},
        }
        if self.collection not in allowed[self.parent_kind]:
            raise ValueError("Collection does not belong to the parent kind")
        return self


class FactView(StrictDTO):
    id: UUID
    kind: FactKind
    source_id: GitHubID
    number: GitHubID | None
    version_key: str
    source_updated_at: UTCDateTime | None
    observed_at: UTCDateTime
    source_url: str | None
    unavailable: bool
    payload: FactPayload

    @field_validator("payload", mode="before")
    @classmethod
    def typed_payload(cls, value, info):
        return validate_fact_payload(info.data["kind"], value)


class FactCoverage(StrictDTO):
    stream: Literal["issues", "pulls", "actions"]
    status: Literal[
        "not_synced",
        "pending",
        "running",
        "complete",
        "partial",
        "failed",
        "permission_missing",
    ]
    start: UTCDateTime | None = None
    end: UTCDateTime | None = None
    completed_watermark: UTCDateTime | None = None
    last_success_at: UTCDateTime | None = None
    error_code: str | None = None

    @field_validator("error_code")
    @classmethod
    def safe_error(cls, value):
        # Job schemas use StrictDTO above; resolve the finite union after imports.
        from devflow.jobs.schemas import JobErrorCode

        return (
            TypeAdapter(JobErrorCode | CollectionReason).validate_python(value)
            if value is not None
            else None
        )


class CollectionView(StrictDTO):
    collection: CollectionKind
    version_key: str
    complete: bool
    reason: CollectionReason | None
    observed_at: UTCDateTime | None


class FactPage(StrictDTO):
    items: list[FactView]
    next_cursor: str | None = None
    coverage: FactCoverage
    collection: CollectionView | None = None


class FactDetail(StrictDTO):
    item: FactView
    collections: list[CollectionView]
    coverage: FactCoverage
