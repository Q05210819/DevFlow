"""Bounded public chat contracts. Private scopes never leave the server."""

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field

from devflow.facts.schemas import StrictDTO

Count = Annotated[int, Field(strict=True, ge=0)]
PositiveCount = Annotated[int, Field(strict=True, gt=0)]


class ConversationView(StrictDTO):
    id: UUID
    selected_repository_ids: tuple[UUID, ...]
    archived: bool
    created_at: datetime


class RunReceipt(StrictDTO):
    run_id: UUID
    job_id: UUID
    message_id: UUID


class MessageView(StrictDTO):
    id: UUID
    role: Literal["user", "assistant"]
    body: str
    ordinal: Count
    created_at: datetime


class MessagePage(StrictDTO):
    """Newest first; the cursor is the oldest delivered ordinal."""

    messages: tuple[MessageView, ...]
    next_cursor: str | None


class Reservation(StrictDTO):
    """The full input and output ceiling held before any request is sent."""

    receipt_id: UUID
    amount: Decimal
    currency: str
    pricing_version: str
    input_cap: PositiveCount
    output_cap: PositiveCount


class ProviderUsage(StrictDTO):
    """Trusted provider usage; reasoning is included in ``completion_tokens``."""

    prompt_tokens: Count
    completion_tokens: Count
    request_id: Annotated[str, Field(strict=True, min_length=1, max_length=128)]
    complete: Annotated[bool, Field(strict=True)]
