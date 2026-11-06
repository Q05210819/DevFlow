from typing import Literal
from uuid import UUID

from pydantic import Field

from devflow.facts.schemas import (
    SHA,
    CollectionReason,
    FactCoverage,
    StrictDTO,
    UTCDateTime,
)
from devflow.jobs.schemas import JobErrorCode
from devflow.workspace.schemas import WorkspaceSelection


class WorkspaceEvidence(StrictDTO):
    selection: WorkspaceSelection
    workspace_id: UUID | None = None
    source_repository_id: UUID | None = None
    commit_sha: SHA | None = None
    availability: Literal["available", "unprepared", "unavailable", "unknown"]
    reason: (
        Literal[
            "not_prepared",
            "version_unavailable",
            "scope_unavailable",
            "verification_limit",
            "verification_unavailable",
            "version_changed",
        ]
        | None
    ) = None
    checked_at: UTCDateTime | None = None
    basis: Literal["local_pin_commit_root_tree_identity"] = (
        "local_pin_commit_root_tree_identity"
    )


class WorkspaceSelectionSummary(StrictDTO):
    selection_order: Literal["default; PR number/base/head; frozen CI order"] = (
        "default; PR number/base/head; frozen CI order"
    )
    basis: Literal["frozen_health_visible_evidence"] = "frozen_health_visible_evidence"
    total_candidates: int = Field(ge=0, le=401)
    returned_count: int = Field(ge=0, le=401)
    truncated: bool = False
    checked_pins: int = Field(ge=0, le=16)
    checked_count: int = Field(ge=0, le=401)
    unverified_count: int = Field(ge=0, le=401)


class HealthRefresh(StrictDTO):
    pass


class PopulationCounts(StrictDTO):
    basis: Literal["observed_persisted_population"] = "observed_persisted_population"
    open_issues: int | None
    open_pulls: int | None
    unassigned_issues: int | None
    draft_pulls: int | None
    stale_issues: int | None
    stale_pulls: int | None


class ActivityWindow(StrictDTO):
    stream: Literal["issues", "pulls", "actions"]
    days: Literal[7, 30]
    requested_start: UTCDateTime
    requested_end: UTCDateTime
    actual_start: UTCDateTime | None
    actual_end: UTCDateTime | None
    complete: bool
    created_records: int | None
    updated_records: int | None


class PRVersion(StrictDTO):
    source_id: int
    number: int
    base_sha: SHA
    head_sha: SHA
    source_url: str | None
    observed_at: UTCDateTime


class CIRunVersion(StrictDTO):
    source_id: int
    attempt: int
    workflow_id: int
    head_sha: SHA
    branch: str | None
    status: str
    conclusion: str | None
    source_url: str | None
    source_updated_at: UTCDateTime | None
    observed_at: UTCDateTime
    default_branch_latest: bool
    pr_numbers: list[int] = Field(max_length=100)


class Selection(StrictDTO):
    selection_order: str
    total_candidates: int
    returned_count: int
    truncated: bool


class InputPopulation(StrictDTO):
    kind: str
    count: int
    observed_from: UTCDateTime | None
    observed_until: UTCDateTime | None


class HealthManifest(StrictDTO):
    rule_version: Literal["health-v1"] = "health-v1"
    frozen_at: UTCDateTime
    sync_generation: int
    requested_sequence: int
    processed_sequence: int
    populations: list[InputPopulation] = Field(max_length=3)
    coverage: list[FactCoverage] = Field(max_length=3)
    pulls: list[PRVersion] = Field(max_length=100)
    pulls_selection: Selection
    actions: list[CIRunVersion] = Field(max_length=200)
    actions_selection: Selection
    unlisted_evidence: Literal["unknown"] = "unknown"
    workspace_versions: list[WorkspaceEvidence] = Field(
        default_factory=list, max_length=401
    )
    workspace_selection: WorkspaceSelectionSummary | None = None


class HealthCounts(PopulationCounts):
    activity_basis: Literal["observed_record_creation_update_not_event_history"] = (
        "observed_record_creation_update_not_event_history"
    )
    activity: list[ActivityWindow] = Field(max_length=6)
    actions_categories: dict[str, int] | None


class HealthSnapshotView(StrictDTO):
    id: UUID
    generation: int
    rule_version: Literal["health-v1"] = "health-v1"
    generated_at: UTCDateTime
    thresholds: dict[str, int]
    counts: HealthCounts
    coverage: list[FactCoverage] = Field(max_length=3)
    manifest: HealthManifest
    workspace: Literal["available", "partial", "unavailable", "unknown"] = "unknown"


class HealthView(StrictDTO):
    snapshot: HealthSnapshotView | None = None
    refresh_status: Literal[
        "idle", "queued", "running", "retry_wait", "completed", "failed"
    ]
    error_code: JobErrorCode | CollectionReason | None = None
    retry_at: UTCDateTime | None = None
