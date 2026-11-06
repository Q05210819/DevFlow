"""Repository-scoped knowledge transport: bounded ingress and read projections."""

from typing import Annotated
from urllib.parse import unquote_to_bytes
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from devflow.auth.dependencies import require_actor, require_csrf
from devflow.auth.types import Actor
from devflow.errors import AppError
from devflow.facts.schemas import SHA
from devflow.github.client import not_found
from devflow.jobs.schemas import IdempotencyKey
from devflow.knowledge import queries
from devflow.knowledge.citations import CitationService
from devflow.knowledge.milvus import MilvusIndex
from devflow.knowledge.retrieval import RetrievalService
from devflow.knowledge.schemas import (
    DeleteImportRequest,
    DocumentView,
    ImportReceipt,
    ImportRequest,
    IndexReceipt,
    IndexRequest,
    KnowledgeView,
    PathPage,
    QueryReceipt,
    QueryRequest,
    QueryView,
    ReplacementRequest,
    SourcePreview,
    StageReceipt,
    UploadImportRequest,
    WorkspaceImportRequest,
    upload_filename,
)
from devflow.knowledge.service import KnowledgeService
from devflow.knowledge.sources import SourceReader
from devflow.pagination import CursorCodec
from devflow.repositories.routes import get_repository_service
from devflow.security import read_secret_file
from devflow.workspace.read import WorkspaceReader

router = APIRouter(
    prefix="/api/v1/repositories/{repository_id}/knowledge", tags=["knowledge"]
)

MAX_UPLOAD_BYTES = 1048576
MAX_QUERY_BYTES = 8192
DEFAULT_DOCUMENT_PATHS = (
    "README",
    "README.md",
    "README.txt",
    "README.rst",
    "docs",
)


def services(request):
    repositories = get_repository_service(request)
    codec = CursorCodec(
        read_secret_file(repositories.policy.settings.app_signing_key_file)
    )
    workspace = WorkspaceReader(
        repositories.factory, repositories.policy, request.app.state.workspace_cache
    )
    reader = SourceReader(
        repositories.factory, repositories.policy, workspace=workspace, codec=codec
    )
    # One lazy adapter per app process; it opens no connection until a read
    # actually has a published index to verify.
    index = getattr(request.app.state, "knowledge_index", None)
    if index is None:
        index = MilvusIndex(repositories.policy.settings)
        request.app.state.knowledge_index = index
    service = KnowledgeService(
        repositories.factory, repositories.policy, reader=reader, index=index
    )
    return service, codec


def query_services(request):
    service, _ = services(request)
    settings = service.policy.settings
    return queries.QueryService(
        service.factory,
        service.policy,
        retrieval=RetrievalService(
            service.factory, service.policy, index=service.index
        ),
        citations=CitationService(
            service.factory,
            service.policy,
            public_base_url=settings.public_base_url,
            reader=service.reader,
        ),
    )


def bounded_query(request):
    """Refuse oversized or non-UTF-8 query metadata before it selects anything."""
    raw = request.scope.get("query_string", b"")
    if len(raw) > MAX_QUERY_BYTES:
        raise AppError("validation_error", 422, "Query exceeds budget")
    try:
        unquote_to_bytes(raw).decode("utf-8")
    except UnicodeError:
        raise AppError("validation_error", 422, "Invalid query encoding") from None


async def _run(call):
    try:
        return await call()
    except SQLAlchemyError:
        raise AppError("service_unavailable", 503, "Service unavailable") from None


@router.get("")
async def overview(
    repository_id: UUID,
    request: Request,
    actor: Annotated[Actor, Depends(require_actor)],
    limit: Annotated[int, Query(ge=1, le=100)] = 30,
    cursor: str | None = None,
) -> KnowledgeView:
    bounded_query(request)
    service, codec = services(request)
    return await _run(
        lambda: queries.overview(
            service, actor, repository_id, limit=limit, cursor=cursor, codec=codec
        )
    )


@router.get("/documents/{document_id}")
async def document(
    repository_id: UUID,
    document_id: UUID,
    request: Request,
    actor: Annotated[Actor, Depends(require_actor)],
    revision_id: UUID | None = None,
    revision_limit: Annotated[int, Query(ge=1, le=100)] = 30,
    revision_cursor: str | None = None,
    start_line: Annotated[int, Query(ge=1)] = 1,
    line_count: Annotated[int, Query(ge=1, le=500)] = 500,
    cursor: str | None = None,
) -> DocumentView:
    bounded_query(request)
    service, codec = services(request)
    return await _run(
        lambda: queries.document_view(
            service,
            actor,
            repository_id,
            document_id,
            revision_id=revision_id,
            revision_limit=revision_limit,
            revision_cursor=revision_cursor,
            start_line=start_line,
            line_count=line_count,
            cursor=cursor,
            codec=codec,
        )
    )


@router.get("/paths")
async def paths(
    repository_id: UUID,
    request: Request,
    actor: Annotated[Actor, Depends(require_actor)],
    workspace_id: UUID,
    sha: SHA,
    document_paths: Annotated[list[str] | None, Query()] = None,
    code_paths: Annotated[list[str] | None, Query()] = None,
    document_paths_explicit: bool = False,
    limit: Annotated[int, Query(ge=1, le=100)] = 16,
    cursor: str | None = None,
) -> PathPage:
    bounded_query(request)
    selected_document_paths = (
        tuple(document_paths or ())
        if document_paths is not None
        else (() if document_paths_explicit else DEFAULT_DOCUMENT_PATHS)
    )
    try:
        selection = WorkspaceImportRequest(
            kind="workspace",
            repository_id=repository_id,
            workspace_id=workspace_id,
            document_paths=selected_document_paths,
            code_paths=tuple(code_paths or ()),
        )
    except ValidationError:
        raise AppError("validation_error", 422, "Invalid path scopes") from None
    service, codec = services(request)
    return await _run(
        lambda: queries.paths_page(
            service,
            actor,
            repository_id,
            workspace_id=workspace_id,
            sha=sha,
            document_paths=selection.document_paths,
            code_paths=selection.code_paths,
            limit=limit,
            cursor=cursor,
            codec=codec,
        )
    )


@router.post("/imports", status_code=202, dependencies=[Depends(require_csrf)])
async def create_import(
    repository_id: UUID,
    request: Request,
    body: ImportRequest,
    actor: Annotated[Actor, Depends(require_actor)],
    idempotency_key: Annotated[IdempotencyKey, Header(alias="Idempotency-Key")],
) -> ImportReceipt:
    bounded_query(request)
    if body.repository_id != repository_id:
        raise AppError("validation_error", 422, "Repository mismatch")
    service, _ = services(request)
    return await _run(lambda: service.request_import(actor, body, idempotency_key))


@router.post("/indexes", status_code=202, dependencies=[Depends(require_csrf)])
async def create_index(
    repository_id: UUID,
    request: Request,
    body: IndexRequest,
    actor: Annotated[Actor, Depends(require_actor)],
    idempotency_key: Annotated[IdempotencyKey, Header(alias="Idempotency-Key")],
) -> IndexReceipt:
    bounded_query(request)
    if body.repository_id != repository_id:
        raise AppError("validation_error", 422, "Repository mismatch")
    from devflow.knowledge.indexing import IndexBuilder

    service, _ = services(request)
    return await _run(
        lambda: IndexBuilder(service).request(
            actor,
            body.import_id,
            idempotency_key,
            repository_id=repository_id,
        )
    )


@router.post("/uploads", status_code=201, dependencies=[Depends(require_csrf)])
async def stage_upload(
    repository_id: UUID,
    request: Request,
    actor: Annotated[Actor, Depends(require_actor)],
    idempotency_key: Annotated[IdempotencyKey, Header(alias="Idempotency-Key")],
    filename: str,
    document_id: UUID | None = None,
) -> StageReceipt:
    bounded_query(request)
    service, _ = services(request)
    # Fresh administrator and base repository access precede any validation
    # oracle and stream consumption.
    await _run(lambda: service.authorize_ingress(actor, repository_id))
    try:
        upload_filename(filename)
    except (ValueError, ValidationError):
        raise AppError("validation_error", 422, "Invalid upload filename") from None
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > MAX_UPLOAD_BYTES:
            raise AppError("payload_too_large", 413, "Upload exceeds 1 MiB")
        chunks.append(chunk)
    body = b"".join(chunks)
    try:
        if b"\0" in body:
            raise ValueError
        body.decode("utf-8")
    except (UnicodeError, ValueError):
        raise AppError(
            "validation_error", 422, "Upload must be valid UTF-8 text"
        ) from None
    return await _run(
        lambda: service.stage_upload_http(
            actor,
            repository_id,
            filename,
            body,
            document_id=document_id,
            idempotency_key=idempotency_key,
        )
    )


@router.post(
    "/documents/{document_id}/replacements",
    status_code=202,
    dependencies=[Depends(require_csrf)],
)
async def replace_document(
    repository_id: UUID,
    document_id: UUID,
    request: Request,
    body: ReplacementRequest,
    actor: Annotated[Actor, Depends(require_actor)],
    idempotency_key: Annotated[IdempotencyKey, Header(alias="Idempotency-Key")],
) -> ImportReceipt:
    bounded_query(request)
    try:
        replacement = UploadImportRequest(
            kind="upload",
            repository_id=repository_id,
            document_id=document_id,
            filename=body.filename,
            staging_id=body.staging_id,
        )
    except ValidationError:
        raise AppError("validation_error", 422, "Invalid replacement") from None
    service, _ = services(request)
    return await _run(
        lambda: service.request_import(actor, replacement, idempotency_key)
    )


@router.post(
    "/documents/{document_id}/deletions",
    status_code=202,
    dependencies=[Depends(require_csrf)],
)
async def delete_document(
    repository_id: UUID,
    document_id: UUID,
    request: Request,
    actor: Annotated[Actor, Depends(require_actor)],
    idempotency_key: Annotated[IdempotencyKey, Header(alias="Idempotency-Key")],
    body: DeleteImportRequest | None = None,
) -> ImportReceipt:
    bounded_query(request)
    if body is not None and (
        body.repository_id != repository_id or body.document_id != document_id
    ):
        raise AppError("validation_error", 422, "Body and route disagree")
    deletion = DeleteImportRequest(
        kind="delete", repository_id=repository_id, document_id=document_id
    )
    service, _ = services(request)
    return await _run(lambda: service.request_import(actor, deletion, idempotency_key))


@router.post("/queries", status_code=202, dependencies=[Depends(require_csrf)])
async def submit_query(
    repository_id: UUID,
    request: Request,
    body: QueryRequest,
    actor: Annotated[Actor, Depends(require_actor)],
    idempotency_key: Annotated[IdempotencyKey, Header(alias="Idempotency-Key")],
) -> QueryReceipt:
    bounded_query(request)
    if repository_id not in body.selection.selected_repository_ids:
        raise AppError("validation_error", 422, "Repository mismatch")
    service = query_services(request)
    return await _run(
        lambda: service.submit(actor, body.selection, body.question, idempotency_key)
    )


@router.get("/queries/{query_id}")
async def read_query(
    repository_id: UUID,
    query_id: UUID,
    request: Request,
    actor: Annotated[Actor, Depends(require_actor)],
) -> QueryView:
    bounded_query(request)
    service = query_services(request)
    view = await _run(lambda: service.get(actor, query_id))
    if repository_id not in view.selection.selected_repository_ids:
        raise not_found()
    return view


@router.get("/queries/{query_id}/citations/{citation_id}")
async def preview_citation(
    repository_id: UUID,
    query_id: UUID,
    citation_id: str,
    request: Request,
    actor: Annotated[Actor, Depends(require_actor)],
    cursor: str | None = None,
) -> SourcePreview:
    bounded_query(request)
    service = query_services(request)
    view = await _run(lambda: service.get(actor, query_id))
    if (
        view.manifest is None
        or repository_id not in view.selection.selected_repository_ids
    ):
        raise not_found()
    return await _run(
        lambda: service.citations.preview(
            actor, view.manifest, citation_id, cursor=cursor
        )
    )
