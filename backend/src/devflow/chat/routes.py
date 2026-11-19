"""Private conversation transport: bounded ingress and creator-only reads.

Every endpoint re-authorizes the conversation's full source set through
``ChatService``; the transport itself never trusts an identifier. SSE progress is
delivered by the existing Job event route, and the final answer is read back from
the private message history, never from a stream.
"""

from typing import Annotated
from urllib.parse import unquote_to_bytes
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request

from devflow.auth.dependencies import require_actor, require_csrf
from devflow.auth.types import Actor
from devflow.chat.schemas import (
    ConversationCreate,
    ConversationList,
    ConversationView,
    DeletionReceipt,
    MessagePage,
    MessageSubmission,
    RunReceipt,
)
from devflow.chat.service import DEFAULT_HISTORY_LIMIT, MAX_HISTORY_LIMIT, ChatService
from devflow.errors import AppError
from devflow.jobs.schemas import IdempotencyKey
from devflow.knowledge.citations import CitationService
from devflow.knowledge.schemas import SourcePreview
from devflow.knowledge.sources import SourceReader
from devflow.pagination import CursorCodec
from devflow.security import read_secret_file
from devflow.workspace.read import WorkspaceReader

router = APIRouter(prefix="/api/v1/conversations", tags=["conversations"])

MAX_QUERY_BYTES = 8192


def get_chat_service(request: Request) -> ChatService:
    service = getattr(request.app.state, "chat_service", None)
    if service is None:
        raise AppError("service_unavailable", 503, "Service unavailable")
    return service


def citation_service(request: Request) -> CitationService:
    """Build the private preview reader only where a citation is requested."""
    policy = request.app.state.repository_policy
    if policy is None:
        raise AppError("service_unavailable", 503, "Service unavailable")
    codec = CursorCodec(read_secret_file(policy.settings.app_signing_key_file))
    workspace = WorkspaceReader(
        request.app.state.session_factory,
        policy,
        request.app.state.workspace_cache,
    )
    return CitationService(
        request.app.state.session_factory,
        policy,
        public_base_url=policy.settings.public_base_url,
        reader=SourceReader(
            request.app.state.session_factory,
            policy,
            workspace=workspace,
            codec=codec,
        ),
    )


def bounded_query(request) -> None:
    """Refuse oversized or non-UTF-8 query metadata before it selects anything."""
    raw = request.scope.get("query_string", b"")
    if len(raw) > MAX_QUERY_BYTES:
        raise AppError("validation_error", 422, "Query exceeds budget")
    try:
        unquote_to_bytes(raw).decode("utf-8")
    except UnicodeError:
        raise AppError("validation_error", 422, "Invalid query encoding") from None


async def _run(call):
    from sqlalchemy.exc import SQLAlchemyError

    try:
        return await call()
    except SQLAlchemyError:
        raise AppError("service_unavailable", 503, "Service unavailable") from None


@router.post("", status_code=201, dependencies=[Depends(require_csrf)])
async def create_conversation(
    request: Request,
    body: ConversationCreate,
    actor: Annotated[Actor, Depends(require_actor)],
    idempotency_key: Annotated[IdempotencyKey, Header(alias="Idempotency-Key")],
) -> ConversationView:
    bounded_query(request)
    service = get_chat_service(request)
    return await _run(
        lambda: service.create(actor, body.selected_repository_ids, idempotency_key)
    )


@router.get("")
async def list_conversations(
    request: Request,
    actor: Annotated[Actor, Depends(require_actor)],
) -> ConversationList:
    bounded_query(request)
    return await _run(lambda: get_chat_service(request).conversations(actor))


@router.get("/{conversation_id}/messages")
async def read_messages(
    conversation_id: UUID,
    request: Request,
    actor: Annotated[Actor, Depends(require_actor)],
    limit: Annotated[int, Query(ge=1, le=MAX_HISTORY_LIMIT)] = DEFAULT_HISTORY_LIMIT,
    cursor: str | None = None,
) -> MessagePage:
    bounded_query(request)
    service = get_chat_service(request)
    return await _run(
        lambda: service.history(actor, conversation_id, cursor=cursor, limit=limit)
    )


@router.post(
    "/{conversation_id}/messages",
    status_code=202,
    dependencies=[Depends(require_csrf)],
)
async def submit_message(
    conversation_id: UUID,
    request: Request,
    body: MessageSubmission,
    actor: Annotated[Actor, Depends(require_actor)],
    idempotency_key: Annotated[IdempotencyKey, Header(alias="Idempotency-Key")],
) -> RunReceipt:
    bounded_query(request)
    service = get_chat_service(request)
    return await _run(
        lambda: service.submit(
            actor,
            conversation_id,
            body.selection,
            body.question,
            idempotency_key,
        )
    )


@router.get("/{conversation_id}/messages/{message_id}/citations/{citation_id}")
async def preview_citation(
    conversation_id: UUID,
    message_id: UUID,
    citation_id: str,
    request: Request,
    actor: Annotated[Actor, Depends(require_actor)],
    cursor: str | None = None,
) -> SourcePreview:
    bounded_query(request)
    service = get_chat_service(request)
    citations = citation_service(request)
    return await _run(
        lambda: service.citation(
            actor,
            conversation_id,
            message_id,
            citation_id,
            citations=citations,
            cursor=cursor,
        )
    )


@router.post(
    "/{conversation_id}/deletions",
    status_code=202,
    dependencies=[Depends(require_csrf)],
)
async def delete_conversation(
    conversation_id: UUID,
    request: Request,
    actor: Annotated[Actor, Depends(require_actor)],
    idempotency_key: Annotated[IdempotencyKey, Header(alias="Idempotency-Key")],
) -> DeletionReceipt:
    bounded_query(request)
    service = get_chat_service(request)
    return await _run(lambda: service.delete(actor, conversation_id, idempotency_key))
