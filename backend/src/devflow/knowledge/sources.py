"""Bounded original-source reads, canonical preview pins and immutable cursors."""

import asyncio
import hashlib
import json
import logging
import re
import time
from contextlib import asynccontextmanager
from datetime import timedelta
from uuid import UUID, uuid4

from sqlalchemy import delete, func, select
from sqlalchemy.orm import defer

from devflow.errors import AppError
from devflow.github.client import not_found
from devflow.knowledge.access import KnowledgeAccess
from devflow.knowledge.chunking import location
from devflow.knowledge.models import (
    Document,
    DocumentRevision,
    ImportMember,
    ImportRepositoryEpoch,
    KnowledgeCorpus,
    KnowledgeImport,
    RevisionDependency,
    RevisionPin,
    UploadStage,
)
from devflow.knowledge.schemas import (
    PATH_RULE_VERSION,
    SourceBytes,
    SourceIdentity,
    SourcePreview,
    WorkspaceImportRequest,
)
from devflow.knowledge.service import KnowledgeService
from devflow.pagination import CursorCodec
from devflow.security import read_secret_file
from devflow.workspace.read import text_kind, valid_path

DOCUMENT_PATHS = ("README", "README.md", "README.txt", "README.rst", "docs")
CODE_SUFFIXES = frozenset(
    [
        ".py",
        ".pyi",
        ".js",
        ".mjs",
        ".cjs",
        ".jsx",
        ".ts",
        ".tsx",
        ".go",
        ".rs",
        ".java",
        ".kt",
        ".c",
        ".h",
        ".cc",
        ".cpp",
        ".hpp",
        ".cs",
        ".rb",
        ".php",
        ".sh",
        ".sql",
        ".vue",
        ".svelte",
    ]
)
EXCLUDED_DIRECTORIES = frozenset(
    [
        "node_modules",
        "vendor",
        "venv",
        "env",
        "__pycache__",
        "dist",
        "build",
        "out",
        "target",
        "coverage",
        "htmlcov",
    ]
)
CREDENTIAL_NAMES = frozenset(
    [
        "id_rsa",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
        "credentials",
        "credentials.json",
        "credentials.yml",
        "credentials.yaml",
        "secrets.json",
        "secrets.yml",
        "secrets.yaml",
    ]
)
CREDENTIAL_SUFFIXES = frozenset([".pem", ".key", ".p12", ".pfx", ".keystore", ".jks"])
logger = logging.getLogger(__name__)


def ascii_lower(text):
    return text.translate(
        str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")
    )


def path_reason(path, *, document_paths=DOCUMENT_PATHS, code_paths=()):
    """None means selected by knowledge-paths-v1; otherwise a stable item reason."""
    if not valid_path(path):
        return "unsupported_path"
    parts = path.split("/")
    folded = [ascii_lower(part) for part in parts]
    suffix = "." + folded[-1].rsplit(".", 1)[1] if "." in folded[-1] else ""
    if any(part.startswith(".") for part in parts):
        return "excluded_hidden"
    if set(folded[:-1]) & EXCLUDED_DIRECTORIES:
        return "excluded_directory"
    if folded[-1] in CREDENTIAL_NAMES or suffix in CREDENTIAL_SUFFIXES:
        return "excluded_credential"

    def selected(scopes):
        return any(
            scope == "" or path == scope or path.startswith(scope + "/")
            for scope in scopes
        )

    root_readme = len(parts) == 1 and folded[-1] in {
        ascii_lower(p) for p in DOCUMENT_PATHS[:-1]
    }
    documents = selected(document_paths) or (
        root_readme and any(ascii_lower(p) == folded[-1] for p in document_paths)
    )
    if documents and (suffix in {".md", ".txt", ".rst"} or root_readme):
        return None
    if selected(code_paths) and suffix in CODE_SUFFIXES:
        return None
    return "unsupported" if documents or selected(code_paths) else "not_selected"


def workspace_source_version(
    *,
    repository_id,
    source_repository_id,
    dependency_repository_ids,
    workspace_id,
    sha,
    path,
):
    dependencies = set(dependency_repository_ids)
    if (
        any(
            not isinstance(value, UUID)
            for value in (
                repository_id,
                source_repository_id,
                workspace_id,
                *dependencies,
            )
        )
        or not {repository_id, source_repository_id} <= dependencies
        or not isinstance(sha, str)
        or not re.fullmatch(r"[0-9a-f]{40}", sha)
        or not valid_path(path)
    ):
        raise ValueError("Invalid Workspace source descriptor")
    descriptor = {
        "version": "knowledge-workspace-source-v1",
        "repository_id": str(repository_id),
        "source_repository_id": str(source_repository_id),
        "dependency_repository_ids": sorted(map(str, dependencies)),
        "workspace_id": str(workspace_id),
        "sha": sha,
        "path": path,
    }
    return hashlib.sha256(
        json.dumps(
            descriptor,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def changed():
    return AppError("version_changed", 409, "Source version changed")


def invalid_cursor():
    return AppError("invalid_cursor", 400, "Invalid preview cursor")


def body_valid(body):
    if not isinstance(body, bytes) or len(body) > 1048576 or text_kind(body) != "text":
        raise AppError("validation_error", 422, "Unsupported source bytes")
    return body


class SourceReader:
    def __init__(self, factory, policy, workspace=None, codec=None):
        self.factory, self.policy, self.workspace = factory, policy, workspace
        self.access = KnowledgeAccess(policy)
        self.service = KnowledgeService(factory, policy)
        self.codec = codec

    async def _lookup(self, db, identity):
        document = await db.get(Document, identity.document_id, with_for_update=True)
        if document is None:
            raise not_found()
        if (document.repository_id, document.source_repository_id) != (
            identity.repository_id,
            identity.source_repository_id,
        ):
            raise not_found()
        if document.path != identity.path or document.kind != (
            "workspace" if identity.workspace_id else "upload"
        ):
            raise changed()
        source_version = identity.revision_hash
        if identity.workspace_id:
            source_version = workspace_source_version(
                **identity.model_dump(exclude={"document_id", "revision_hash"})
            )
        revision = await db.scalar(
            select(DocumentRevision)
            .options(defer(DocumentRevision.body))
            .where(
                DocumentRevision.document_id == document.id,
                DocumentRevision.revision_hash == identity.revision_hash,
                DocumentRevision.source_version == source_version,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if revision is None or revision.published_at is None:
            raise not_found()
        if (revision.workspace_id, revision.sha) != (
            identity.workspace_id,
            identity.sha,
        ):
            raise changed()
        dependencies = set(
            await db.scalars(
                select(RevisionDependency.repository_id).where(
                    RevisionDependency.revision_id == revision.id
                )
            )
        )
        if dependencies != set(identity.dependency_repository_ids):
            raise changed()
        reason = (
            "source_unavailable"
            if document.deleted or revision.availability in {"deleted", "unavailable"}
            else "source_expired"
            if revision.availability == "expired"
            else None
        )
        if reason is None and await db.scalar(
            select(DocumentRevision.body.is_(None)).where(
                DocumentRevision.id == revision.id
            )
        ):
            reason = "source_expired"
        return revision, reason

    async def _current(self, db, revision):
        return bool(
            revision.searchable
            and revision.availability == "available"
            and revision.published_at
            and await db.scalar(
                select(ImportMember.id)
                .join(KnowledgeImport, ImportMember.import_id == KnowledgeImport.id)
                .join(
                    KnowledgeCorpus,
                    KnowledgeImport.repository_id == KnowledgeCorpus.repository_id,
                )
                .where(
                    ImportMember.revision_id == revision.id,
                    ImportMember.status == "included",
                    KnowledgeImport.status.in_(("ready", "ready_partial")),
                    KnowledgeImport.target_generation
                    == KnowledgeCorpus.target_generation,
                )
                .limit(1)
            )
        )

    async def _materialize(self, revision_id):
        async with self.factory() as db:
            return await db.scalar(
                select(DocumentRevision.body).where(DocumentRevision.id == revision_id)
            )

    @asynccontextmanager
    async def _pinned(self, actor, identity, deadline):
        grant = await self.access.verify(
            actor, identity.dependency_repository_ids, fresh=False
        )
        pin_id, revision_id = None, None
        try:
            async with self.factory.begin() as db:
                await self.access.lock(db, grant)
                revision, reason = await self._lookup(db, identity)
                revision_id = revision.id
                current = await self._current(db, revision) if reason is None else False
                if reason is None:
                    now = await db.scalar(select(func.clock_timestamp()))
                    pin_id = uuid4()
                    db.add(
                        RevisionPin(
                            id=pin_id,
                            revision_id=revision.id,
                            expires_at=now + timedelta(seconds=30),
                        )
                    )
            body = await self._materialize(revision_id) if reason is None else None
            if reason is None and body is None:
                reason = "source_expired"
            if body is not None:
                body_valid(body)
                if hashlib.sha256(body).hexdigest() != identity.revision_hash:
                    raise changed()
            yield body, reason, current
            # Check after caller has materialized its bounded result, before return.
            async with self.factory.begin() as db:
                await self.access.lock(db, grant)
                revision, final_reason = await self._lookup(db, identity)
                if (
                    revision.id != revision_id
                    or final_reason != (None if body is not None else reason)
                    or (
                        await self._current(db, revision)
                        if final_reason is None
                        else False
                    )
                    != current
                ):
                    raise changed()
                if pin_id:
                    pin = await db.get(RevisionPin, pin_id)
                    now = await db.scalar(select(func.clock_timestamp()))
                    if (
                        pin is None
                        or pin.revision_id != revision_id
                        or pin.expires_at <= now
                    ):
                        raise changed()
        finally:
            remaining = min(1, max(0, deadline - time.monotonic()))
            if pin_id and remaining:

                async def release():
                    try:
                        async with (
                            asyncio.timeout(remaining),
                            self.factory.begin() as db,
                        ):
                            await db.execute(
                                delete(RevisionPin).where(
                                    RevisionPin.id == pin_id,
                                    RevisionPin.revision_id == revision_id,
                                )
                            )
                    except Exception:  # noqa: BLE001 - expiring pin retains bytes on DB outage
                        logger.warning("Knowledge preview pin release unavailable")

                # Release on cancellation as well. Expiry still bounds a DB outage.
                cleanup = asyncio.create_task(release())
                interrupted = False
                while not cleanup.done():
                    try:
                        await asyncio.shield(cleanup)
                    except asyncio.CancelledError:
                        interrupted = True
                if interrupted:
                    raise asyncio.CancelledError

    async def read(self, actor, identity):
        identity = SourceIdentity.model_validate(identity.model_dump())
        deadline = time.monotonic() + 5
        try:
            async with asyncio.timeout(5):
                async with self._pinned(actor, identity, deadline) as (body, reason, _):
                    if reason:
                        raise not_found()
                    if identity.workspace_id:
                        if self.workspace is None:
                            raise AppError(
                                "service_unavailable",
                                503,
                                "Workspace reader unavailable",
                            )
                        original = await self.workspace.raw(
                            actor,
                            identity.repository_id,
                            identity.workspace_id,
                            identity.path,
                            sha=identity.sha,
                        )
                        if original != body:
                            raise changed()
                    result = SourceBytes(identity=identity, body=body)
                return result
        except TimeoutError:
            raise AppError("attempt_timeout", 503, "Source read timed out") from None

    async def preview(
        self, actor, identity, *, start_line=1, line_count=500, cursor=None
    ):
        identity = SourceIdentity.model_validate(identity.model_dump())
        if (
            type(start_line) is not int
            or start_line < 1
            or type(line_count) is not int
            or not 1 <= line_count <= 500
        ):
            raise AppError("validation_error", 422, "Invalid preview range")
        deadline = time.monotonic() + 5
        try:
            async with asyncio.timeout(5):
                codec = self.codec or CursorCodec(
                    read_secret_file(self.policy.settings.app_signing_key_file)
                )
                scope = [identity.model_dump(mode="json"), start_line, line_count]
                purpose = (
                    "knowledge-preview:v1:"
                    + hashlib.sha256(
                        json.dumps(
                            scope,
                            sort_keys=True,
                            separators=(",", ":"),
                            ensure_ascii=False,
                        ).encode()
                    ).hexdigest()
                )
                position = (
                    codec.decode_cursor(cursor, actor.id, purpose, 1)
                    if cursor is not None
                    else None
                )
                if position is not None and (
                    set(position) != {"offset", "expires"}
                    or type(position["offset"]) is not int
                    or type(position["expires"]) is not int
                ):
                    raise invalid_cursor()
                async with self._pinned(actor, identity, deadline) as (
                    body,
                    reason,
                    current,
                ):
                    async with self.factory() as db:
                        now = await db.scalar(select(func.clock_timestamp()))
                        if (
                            position is not None
                            and position["expires"] <= now.timestamp()
                        ):
                            raise invalid_cursor()
                    if reason:
                        result = SourcePreview(
                            identity=identity,
                            text=None,
                            location=None,
                            is_current=False,
                            partial=False,
                            next_cursor=None,
                            reason=reason,
                        )
                    else:
                        starts = [0] + [
                            i + 1
                            for i, byte in enumerate(body)
                            if byte == 10 and i + 1 < len(body)
                        ]
                        begin = (
                            starts[start_line - 1]
                            if start_line <= len(starts)
                            else len(body)
                        )
                        window_end = (
                            starts[start_line - 1 + line_count]
                            if start_line - 1 + line_count < len(starts)
                            else len(body)
                        )
                        offset = position["offset"] if position else begin
                        if (
                            not begin <= offset <= window_end
                            or (position is not None and offset == window_end)
                            or (offset < len(body) and body[offset] & 0xC0 == 0x80)
                        ):
                            raise invalid_cursor()
                        end = min(window_end, offset + 262144)
                        if end < window_end:
                            last_lf = body.rfind(b"\n", offset, end)
                            if last_lf >= offset:
                                end = last_lf + 1
                        while (
                            end > offset
                            and end < len(body)
                            and body[end] & 0xC0 == 0x80
                        ):
                            end -= 1
                        following = None
                        if end < window_end:
                            expiry = (
                                position["expires"]
                                if position
                                else int(now.timestamp()) + 900
                            )
                            following = codec.encode_cursor(
                                actor.id, purpose, {"offset": end, "expires": expiry}, 1
                            )
                        result = SourcePreview(
                            identity=identity,
                            text=body[offset:end].decode("utf-8"),
                            location=location(body, offset, end)
                            if end > offset
                            else None,
                            is_current=current,
                            partial=end < len(body),
                            next_cursor=following,
                        )
                return result
        except TimeoutError:
            raise AppError("attempt_timeout", 503, "Source preview timed out") from None

    async def _import_context(self, actor, import_id, path):
        async with self.factory() as db:
            imported = await db.get(KnowledgeImport, import_id)
            if imported is None:
                raise not_found()
            dependencies = tuple(
                await db.scalars(
                    select(ImportRepositoryEpoch.repository_id)
                    .where(ImportRepositoryEpoch.import_id == import_id)
                    .order_by(ImportRepositoryEpoch.repository_id)
                )
            )
        grant = await self.access.verify(actor, dependencies, fresh=True)
        async with self.factory.begin() as db:
            await self.access.lock(db, grant)
            await self.service._admin(db, actor)
            await self.service._original_epochs(db, imported, grant)
            corpus = await db.get(
                KnowledgeCorpus, imported.repository_id, with_for_update=True
            )
            current = await db.get(
                KnowledgeImport, import_id, with_for_update=True, populate_existing=True
            )
            now = await db.scalar(select(func.clock_timestamp()))
            if (
                current.status not in {"queued", "building"}
                or current.deadline <= now
                or corpus is None
                or corpus.target_generation != current.target_generation
                or current.path_rule_version != PATH_RULE_VERSION
            ):
                raise changed()
            if current.kind == "workspace":
                request = WorkspaceImportRequest.model_validate(current.request)
                if (
                    request.repository_id != current.repository_id
                    or request.workspace_id != current.workspace_id
                    or path_reason(
                        path,
                        document_paths=request.document_paths,
                        code_paths=request.code_paths,
                    )
                    is not None
                ):
                    raise AppError(
                        "validation_error", 422, "Source path is not selected"
                    )
                context = (
                    current.repository_id,
                    current.workspace_id,
                    current.sha,
                    current.source_repository_id,
                    dependencies,
                )
                # The Workspace reader reauthorizes its exact version/scopes under
                # the existing process guard; this descriptor is never a grant.
                from devflow.workspace.models import WorkspaceVersion

                version = await db.get(WorkspaceVersion, current.workspace_id)
                if (
                    version is None
                    or version.repository_id != current.repository_id
                    or version.source_repository_id != current.source_repository_id
                    or version.commit_sha != current.sha
                    or {UUID(scope["repository_id"]) for scope in version.scopes}
                    != set(dependencies)
                ):
                    raise changed()
                body = None
            elif current.kind == "upload":
                if path is not None:
                    raise AppError(
                        "validation_error", 422, "Upload does not have a Git path"
                    )
                request = current.request
                stage = await db.get(
                    UploadStage, UUID(request["staging_id"]), with_for_update=True
                )
                if (
                    stage is None
                    or stage.actor_id != actor.id
                    or stage.consumed_import_id != current.id
                    or stage.filename != request["filename"]
                ):
                    raise not_found()
                member = await db.scalar(
                    select(ImportMember)
                    .where(
                        ImportMember.import_id == current.id, ImportMember.ordinal == 0
                    )
                    .with_for_update()
                )
                if member is None or member.revision_id != stage.revision_id:
                    raise not_found()
                document_id = await db.scalar(
                    select(DocumentRevision.document_id).where(
                        DocumentRevision.id == stage.revision_id
                    )
                )
                document = await db.get(Document, document_id, with_for_update=True)
                if (
                    document is None
                    or document.deleted
                    or document.kind != "upload"
                    or document.repository_id != current.repository_id
                    or document.source_repository_id != current.repository_id
                    or member.source_key != str(document.id)
                    or (
                        request.get("document_id") is not None
                        and request["document_id"] != str(document.id)
                    )
                ):
                    raise not_found()
                revision = await db.scalar(
                    select(DocumentRevision)
                    .options(defer(DocumentRevision.body))
                    .where(DocumentRevision.id == stage.revision_id)
                    .with_for_update()
                )
                if revision is None:
                    raise not_found()
                actual_dependencies = set(
                    await db.scalars(
                        select(RevisionDependency.repository_id).where(
                            RevisionDependency.revision_id == revision.id
                        )
                    )
                )
                if (
                    revision.availability != "available"
                    or actual_dependencies != {document.repository_id}
                    or revision.workspace_id is not None
                    or revision.sha is not None
                    or revision.source_version != revision.revision_hash
                ):
                    raise not_found()
                body = await db.scalar(
                    select(DocumentRevision.body).where(
                        DocumentRevision.id == revision.id
                    )
                )
                if body is None:
                    raise not_found()
                body_valid(body)
                if hashlib.sha256(body).hexdigest() != revision.revision_hash:
                    raise changed()
                context = (
                    document.id,
                    revision.id,
                    revision.revision_hash,
                    dependencies,
                )
            else:
                raise not_found()
            final_now = await db.scalar(select(func.clock_timestamp()))
            if current.deadline <= final_now:
                raise changed()
            return context, body

    async def read_import(self, actor, import_id, *, path=None):
        context, body = await self._import_context(actor, import_id, path)
        if body is None:
            if self.workspace is None:
                raise AppError(
                    "service_unavailable", 503, "Workspace reader unavailable"
                )
            repo, workspace, sha, _, _ = context
            body = await self.workspace.raw(actor, repo, workspace, path, sha=sha)
        final, _ = await self._import_context(actor, import_id, path)
        if final != context:
            raise changed()
        return body_valid(body)

    async def _active_manifest(self, db, import_id, grant):
        await self.access.lock(db, grant)
        await self.service._admin(db, grant.actor)
        imported = await db.get(KnowledgeImport, import_id)
        corpus = await db.get(
            KnowledgeCorpus, imported.repository_id, with_for_update=True
        )
        imported = await db.get(
            KnowledgeImport, import_id, with_for_update=True, populate_existing=True
        )
        await self.service._original_epochs(db, imported, grant)
        now = await db.scalar(select(func.clock_timestamp()))
        if (
            imported.composition_version != 1
            or imported.status not in {"queued", "building"}
            or imported.deadline <= now
            or imported.target_generation != corpus.target_generation
            or imported.path_rule_version != PATH_RULE_VERSION
        ):
            raise changed()
        return imported, now

    async def _manifest_member(self, db, import_id, member_id, grant):
        imported, now = await self._active_manifest(db, import_id, grant)
        member = await db.get(ImportMember, member_id)
        if (
            member is None
            or member.import_id != imported.id
            or member.status != "pending"
            or member.revision_id is None
        ):
            raise not_found()
        document_id = await db.scalar(
            select(DocumentRevision.document_id).where(
                DocumentRevision.id == member.revision_id
            )
        )
        document = await db.get(Document, document_id, with_for_update=True)
        revision = await db.scalar(
            select(DocumentRevision)
            .options(defer(DocumentRevision.body))
            .where(DocumentRevision.id == member.revision_id)
            .with_for_update()
        )
        if (
            document.repository_id != imported.repository_id
            or document.deleted
            or revision.availability != "available"
        ):
            raise not_found()
        dependencies = tuple(
            await db.scalars(
                select(RevisionDependency.repository_id)
                .where(RevisionDependency.revision_id == revision.id)
                .order_by(RevisionDependency.repository_id)
            )
        )
        if member.member_kind == "upload":
            if (
                document.kind != "upload"
                or member.source_key != str(document.id)
                or document.accepted_revision_id != revision.id
                or set(dependencies) != {document.repository_id}
                or revision.workspace_id is not None
                or revision.source_version != revision.revision_hash
            ):
                raise not_found()
        else:
            from devflow.workspace.models import WorkspaceVersion

            version = await db.get(WorkspaceVersion, imported.target_workspace_id)
            if (
                version is None
                or document.kind != "workspace"
                or member.source_key != document.path
                or document.source_repository_id != version.source_repository_id
                or (revision.workspace_id, revision.sha)
                != (version.id, imported.target_sha)
                or set(dependencies)
                != {UUID(scope["repository_id"]) for scope in version.scopes}
            ):
                raise changed()
        identity = SourceIdentity(
            repository_id=document.repository_id,
            source_repository_id=document.source_repository_id,
            dependency_repository_ids=dependencies,
            document_id=document.id,
            revision_hash=revision.revision_hash,
            workspace_id=revision.workspace_id,
            sha=revision.sha,
            path=document.path,
        )
        if (
            identity.workspace_id
            and revision.source_version
            != workspace_source_version(
                **identity.model_dump(exclude={"document_id", "revision_hash"})
            )
        ):
            raise changed()
        return imported, revision, identity, now

    async def read_member(self, actor, import_id, member_id):
        """Internal accepted manifest read; never authorizes a public staged preview."""
        from devflow.knowledge.ingestion import context

        _, grant = await context(self.service, import_id)
        if grant.actor != actor:
            raise not_found()
        pin_id = uuid4()
        try:
            async with asyncio.timeout(5):
                async with self.factory.begin() as db:
                    _, revision, identity, now = await self._manifest_member(
                        db, import_id, member_id, grant
                    )
                    revision_id = revision.id
                    db.add(
                        RevisionPin(
                            id=pin_id,
                            revision_id=revision_id,
                            expires_at=now + timedelta(seconds=30),
                        )
                    )
                body = await self._materialize(revision_id)
                if body is None:
                    raise not_found()
                body_valid(body)
                if hashlib.sha256(body).hexdigest() != identity.revision_hash:
                    raise changed()
                async with self.factory.begin() as db:
                    _, revision, final_identity, now = await self._manifest_member(
                        db, import_id, member_id, grant
                    )
                    pin = await db.get(RevisionPin, pin_id)
                    if (
                        final_identity != identity
                        or revision.id != revision_id
                        or pin is None
                        or pin.expires_at <= now
                    ):
                        raise changed()
                return SourceBytes(identity=identity, body=body)
        finally:

            async def release():
                async with asyncio.timeout(1), self.factory.begin() as db:
                    await db.execute(
                        delete(RevisionPin).where(RevisionPin.id == pin_id)
                    )

            cleanup = asyncio.create_task(release())
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                await cleanup
                raise

    async def _target_context(self, actor, import_id, path):
        from devflow.knowledge.ingestion import context
        from devflow.workspace.models import WorkspaceVersion

        _, grant = await context(self.service, import_id)
        if grant.actor != actor:
            raise not_found()
        async with self.factory.begin() as db:
            imported, _ = await self._active_manifest(db, import_id, grant)
            if path_reason(path, **imported.target_path_scopes) is not None:
                raise not_found()
            version = await db.get(WorkspaceVersion, imported.target_workspace_id)
            if (
                version is None
                or version.commit_sha != imported.target_sha
                or version.repository_id != imported.repository_id
                or not {UUID(scope["repository_id"]) for scope in version.scopes}
                <= {epoch.repository_id for epoch in grant.repository_epochs}
            ):
                raise changed()
            document = await db.scalar(
                select(Document).where(
                    Document.repository_id == imported.repository_id,
                    Document.source_repository_id == version.source_repository_id,
                    Document.kind == "workspace",
                    Document.path == path,
                )
            )
            if document and document.deleted:
                raise changed()
            return imported.repository_id, version.id, version.commit_sha

    async def read_target(self, actor, import_id, path):
        """First bytes for a composed Workspace target, under this import's grant."""
        scope = await self._target_context(actor, import_id, path)
        if self.workspace is None:
            raise AppError("service_unavailable", 503, "Workspace reader unavailable")
        repo, workspace, sha = scope
        async with self.factory() as db:
            member = await db.scalar(
                select(ImportMember).where(
                    ImportMember.import_id == import_id,
                    ImportMember.member_kind == "workspace",
                    ImportMember.source_key == path,
                    ImportMember.status == "pending",
                )
            )
            if member is None or member.blob_oid is None:
                raise not_found()
            oid = member.blob_oid
        body = await self.workspace.raw(
            actor, repo, workspace, path, sha=sha, expected_oid=oid
        )
        if await self._target_context(actor, import_id, path) != scope:
            raise changed()
        return body_valid(body)
