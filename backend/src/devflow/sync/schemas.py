from typing import Literal
from uuid import UUID

from pydantic import Field, model_validator

from devflow.facts.schemas import SHA, GitHubID, StrictDTO, UTCDateTime

SyncStream = Literal["issues", "pulls", "actions"]
SyncStatus = Literal[
    "idle", "queued", "running", "retry_wait", "completed", "failed", "budget_exhausted"
]
CursorStatus = Literal[
    "pending", "running", "complete", "partial", "failed", "permission_missing"
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
    source_id: GitHubID
    number: GitHubID | None = None
    attempt: GitHubID | None = None


class CapturedSHA(StrictDTO):
    base: SHA
    head: SHA


class SyncCheckpoint(StrictDTO):
    stream: SyncStream
    mode: Literal[
        "initial", "incremental", "open", "backfill", "reconcile", "targeted"
    ] = "initial"
    page: int = Field(default=1, strict=True, ge=1)
    window: SyncWindow | None = None
    pending_targets: list[SyncTarget] = Field(default_factory=list, max_length=100)
    target: SyncTarget | None = None
    stage: Literal[
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
    ] = "enumerate"
    child_page: int = Field(default=1, strict=True, ge=1)
    captured_sha: CapturedSHA | None = None
    recaptures: int = Field(default=0, strict=True, ge=0, le=3)
    captured_sequence: int = Field(default=0, strict=True, ge=0)
    target_started_at: UTCDateTime | None = None
    known_after_id: int = Field(default=0, strict=True, ge=0)
    known_complete: bool = False


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
