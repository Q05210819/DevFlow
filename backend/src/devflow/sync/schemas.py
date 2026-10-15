from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, model_validator

from devflow.facts.schemas import (
    SHA,
    CollectionKind,
    CollectionReason,
    GitHubID,
    StrictDTO,
    UTCDateTime,
)
from devflow.jobs.schemas import JobErrorCode

SyncStream = Literal["issues", "pulls", "actions"]
SyncStatus = Literal[
    "idle", "queued", "running", "retry_wait", "completed", "failed", "budget_exhausted"
]
CursorStatus = Literal[
    "pending", "running", "complete", "partial", "failed", "permission_missing"
]
CheckpointStage = Literal[
    "enumerate",
    "detail",
    "comments",
    "reviews",
    "review_comments",
    "files",
    "verify",
    "attempts",
    "jobs",
    "done",
    "issue_identity",
    "repository_proof",
    "permission_proof",
    "missing_verify",
]


class SyncWindow(StrictDTO):
    since: UTCDateTime
    until: UTCDateTime

    @model_validator(mode="after")
    def ordered(self):
        if self.since > self.until:
            raise ValueError("Sync window start must not follow its end")
        return self


class BackfillRange(SyncWindow):
    streams: list[SyncStream] = Field(
        default_factory=lambda: ["issues", "pulls", "actions"],
        min_length=1,
        max_length=3,
    )

    @model_validator(mode="after")
    def unique_streams(self):
        if len(set(self.streams)) != len(self.streams):
            raise ValueError("Backfill streams must be unique")
        return self


class SyncTarget(StrictDTO):
    kind: Literal["issue", "pull", "ci_run"]
    source_id: GitHubID | None = None
    number: GitHubID | None = None
    attempt: GitHubID | None = None

    @model_validator(mode="after")
    def identity(self):
        if self.kind in {"issue", "pull"}:
            if self.number is None or self.attempt is not None:
                raise ValueError("Issue and pull targets require number and no attempt")
        elif self.number is not None:
            raise ValueError("Run target cannot carry an issue number")
        if self.source_id is None and self.kind != "pull":
            raise ValueError("Canonical source ID is required")
        return self


class CapturedSHA(StrictDTO):
    base: SHA
    head: SHA


class PendingCollection(StrictDTO):
    collection: CollectionKind
    reason: CollectionReason | None = None


class SyncCheckpoint(StrictDTO):
    stream: SyncStream
    mode: Literal[
        "initial", "incremental", "open", "backfill", "reconcile", "targeted"
    ] = "initial"
    page: int = Field(default=1, strict=True, ge=1)
    window: SyncWindow | None = None
    pending_targets: list[SyncTarget] = Field(default_factory=list, max_length=100)
    target: SyncTarget | None = None
    stage: CheckpointStage = "enumerate"
    missing_stage: Literal["detail", "issue_identity"] | None = None
    child_page: int = Field(default=1, strict=True, ge=1)
    captured_sha: CapturedSHA | None = None
    recaptures: int = Field(default=0, strict=True, ge=0, le=3)
    recapture_counts: dict[
        Annotated[str, Field(pattern=r"^[1-9][0-9]*$")],
        Annotated[int, Field(strict=True, ge=1, le=3)],
    ] = Field(default_factory=dict, max_length=1000)
    captured_sequence: int = Field(default=0, strict=True, ge=0)
    target_started_at: UTCDateTime | None = None
    known_after_id: int = Field(default=0, strict=True, ge=0)
    known_complete: bool = False
    phase: Literal["open", "window", "known"] = "open"
    enumeration_done: bool = False
    issue_id: GitHubID | None = None
    latest_attempt: GitHubID | None = None
    current_attempt: GitHubID | None = None
    run_head: SHA | None = None
    file_count: int = Field(default=0, strict=True, ge=0, le=3000)
    pending_collections: list[PendingCollection] = Field(
        default_factory=list, max_length=4
    )
    active_window: SyncWindow | None = None
    pending_windows: list[SyncWindow] = Field(default_factory=list, max_length=32)
    unresolved_windows: list[SyncWindow] = Field(default_factory=list, max_length=100)
    gap_reason: CollectionReason | None = None


class SyncDemandInput(StrictDTO):
    repository_id: UUID
    sequence: int = Field(strict=True, ge=1)
    reason: Literal[
        "registration", "refresh", "webhook", "periodic", "reconcile", "backfill"
    ]
    target: SyncTarget | None = None
    backfill: BackfillRange | None = None

    @model_validator(mode="after")
    def consistent_reason(self):
        if (self.reason == "backfill") != (self.backfill is not None):
            raise ValueError("Backfill demand requires a backfill window")
        return self


class SyncRefresh(StrictDTO):
    resume: bool = Field(default=False, strict=True)


class SyncBackfill(StrictDTO):
    start: UTCDateTime
    end: UTCDateTime
    streams: list[SyncStream] = Field(min_length=1, max_length=3)

    @model_validator(mode="after")
    def valid_range(self):
        from datetime import timedelta

        if self.start > self.end or self.end - self.start > timedelta(days=365):
            raise ValueError("Invalid backfill window")
        if len(set(self.streams)) != len(self.streams):
            raise ValueError("Backfill streams must be unique")
        return self


class SyncCoverage(StrictDTO):
    start: UTCDateTime | None = None
    end: UTCDateTime | None = None
    completed_watermark: UTCDateTime | None = None


class SyncRemaining(StrictDTO):
    # Counts describe only persisted work, never the unknown remote total.
    pending_targets: int = Field(ge=0)
    pending_windows: int = Field(ge=0)
    unresolved_windows: int = Field(ge=0)
    current_target: bool
    enumeration_complete: bool | None
    mode: Literal["initial", "incremental", "open", "backfill", "reconcile", "targeted"]
    stage: CheckpointStage
    reason: CollectionReason | None = None


class SyncStreamView(StrictDTO):
    stream: SyncStream
    status: CursorStatus | Literal["not_synced"]
    coverage: SyncCoverage
    last_success_at: UTCDateTime | None = None
    remaining: SyncRemaining | None = None
    error_code: JobErrorCode | CollectionReason | None = None


class SyncView(StrictDTO):
    repository_id: UUID
    requested_sequence: int = Field(ge=0)
    processed_sequence: int = Field(ge=0)
    generation: int = Field(ge=0)
    status: SyncStatus
    streams: list[SyncStreamView]
    error_code: JobErrorCode | CollectionReason | None = None
    next_due_at: UTCDateTime | None = None
    retry_at: UTCDateTime | None = None
