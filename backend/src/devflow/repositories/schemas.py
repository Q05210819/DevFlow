from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from devflow.facts.schemas import CollectionReason, StrictDTO, UTCDateTime
from devflow.jobs.schemas import JobErrorCode
from devflow.sync.schemas import SyncStatus, SyncView

NumericId = Annotated[int, Field(strict=True, gt=0, le=9223372036854775807)]


class CandidateRepository(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    github_repository_id: int
    installation_id: int
    full_name: str
    private: bool
    default_branch: str | None


class RepositoryView(CandidateRepository):
    id: UUID


class RepositorySyncSummary(StrictDTO):
    status: SyncStatus = "idle"
    generation: int = 0
    requested_sequence: int = 0
    processed_sequence: int = 0
    error_code: JobErrorCode | CollectionReason | None = None
    retry_at: UTCDateTime | None = None


class RepositoryListView(RepositoryView):
    sync: RepositorySyncSummary


class RepositoryDetailView(RepositoryView):
    sync: SyncView


class RepositoryRegister(BaseModel):
    model_config = ConfigDict(extra="forbid")
    installation_id: NumericId
    github_repository_id: NumericId
