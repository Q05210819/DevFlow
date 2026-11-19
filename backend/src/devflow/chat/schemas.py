"""Bounded public chat contracts. Private scopes never leave the server."""

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field

from devflow.facts.schemas import StrictDTO
from devflow.knowledge.schemas import QuerySelection

Count = Annotated[int, Field(strict=True, ge=0)]
PositiveCount = Annotated[int, Field(strict=True, gt=0)]


class ConversationView(StrictDTO):
    id: UUID
    selected_repository_ids: tuple[UUID, ...]
    archived: bool
    created_at: datetime


class ConversationCreate(StrictDTO):
    selected_repository_ids: Annotated[
        tuple[UUID, ...], Field(min_length=1, max_length=3)
    ]


class RunSummary(StrictDTO):
    """Only identifiers and a fixed state; never a question or answer body."""

    run_id: UUID
    job_id: UUID
    status: Literal["queued", "running", "succeeded", "failed"]


class ConversationSummary(StrictDTO):
    id: UUID
    selected_repository_ids: tuple[UUID, ...]
    created_at: datetime
    active_run: RunSummary | None = None
    latest_run: RunSummary | None = None


class ConversationList(StrictDTO):
    conversations: tuple[ConversationSummary, ...]


class RunReceipt(StrictDTO):
    run_id: UUID
    job_id: UUID
    message_id: UUID


class MessageSubmission(StrictDTO):
    selection: QuerySelection
    question: Annotated[str, Field(strict=True, min_length=1, max_length=2048)]


class CitationView(StrictDTO):
    """A bounded pointer; previews are fetched from the private citation route."""

    citation_id: str
    url: str
    repository_id: UUID
    source_repository_id: UUID
    path: str | None
    # The frozen version this evidence came from, so the UI can show the round's
    # SHA without trusting a client-supplied value.
    sha: str | None
    start_line: Count
    end_line: Count


class MessageView(StrictDTO):
    id: UUID
    role: Literal["user", "assistant"]
    body: str
    ordinal: Count
    created_at: datetime
    citations: tuple[CitationView, ...] = ()


class MessagePage(StrictDTO):
    """Newest first; the cursor is the oldest delivered ordinal."""

    messages: tuple[MessageView, ...]
    next_cursor: str | None


class DeletionReceipt(StrictDTO):
    """Bodies are already gone; the Job reclaims official checkpoint threads."""

    conversation_id: UUID
    job_id: UUID


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


CitationId = Annotated[str, Field(strict=True, pattern=r"^[0-9a-f]{16,64}$")]


class RunOutcome(StrictDTO):
    """The only terminal outcomes a ChatAgent run may publish.

    ``answer_call_id`` must name the succeeded ``answer`` ToolCall of the same
    run; the assistant body is read back from that trusted record, never from
    model output directly. ``error_code`` is only meaningful for ``failed``.
    """

    status: Literal["answered", "no_evidence", "failed", "cancelled"]
    answer_call_id: UUID | None = None
    error_code: (
        Annotated[str, Field(strict=True, min_length=1, max_length=64)] | None
    ) = None


class AnswerDraft(StrictDTO):
    """The model's only accepted answer shape; citations are re-validated.

    ``citation_ids`` may only name evidence in the exact request that produced
    this answer. The body limit is enforced on the raw content bytes before
    parsing, so this model never sees an oversized response.
    """

    conclusion: Annotated[str, Field(strict=True, min_length=1, max_length=16384)]
    citation_ids: Annotated[tuple[CitationId, ...], Field(max_length=6)] = ()
    insufficient_evidence: Annotated[bool, Field(strict=True)]
    next_steps: Annotated[
        tuple[
            Annotated[str, Field(strict=True, max_length=1024)],
            ...,
        ],
        Field(max_length=8),
    ] = ()
