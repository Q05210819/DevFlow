from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

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


class RepositoryRegister(BaseModel):
    model_config = ConfigDict(extra="forbid")
    installation_id: NumericId
    github_repository_id: NumericId
