from dataclasses import dataclass
from typing import Literal
from uuid import UUID

Role = Literal["admin", "member"]


@dataclass(frozen=True, slots=True)
class Actor:
    id: UUID
    github_id: int
    role: Role
    auth_version: int
