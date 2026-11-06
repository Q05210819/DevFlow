"""Short atomic admissions. Source IO and batch execution belong to later tasks."""

import hashlib
import json
from dataclasses import asdict
from datetime import timedelta
from uuid import uuid4

from pydantic import TypeAdapter, ValidationError
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert

from devflow.audit.service import AuditEntry, write_audit
from devflow.auth.models import User
from devflow.errors import AppError
from devflow.github.client import not_found
from devflow.jobs.models import Job
from devflow.jobs.schemas import IdempotencyKey, KnowledgeImportInput
from devflow.jobs.service import append_event
from devflow.knowledge.access import KnowledgeAccess
from devflow.knowledge.models import (
    Document,
    DocumentRevision,
    ImportMember,
    ImportRepositoryEpoch,
    KnowledgeCapacity,
    KnowledgeCorpus,
    KnowledgeImport,
    RepositoryCapacity,
    RevisionDependency,
    UploadStage,
)
from devflow.knowledge.schemas import (
    ImportReceipt,
    ImportRequest,
    StageReceipt,
    metadata_bytes,
    upload_filename,
)
from devflow.sync.scope import transaction
from devflow.workspace.models import WorkspaceVersion

REQUEST = TypeAdapter(ImportRequest)
KEY = TypeAdapter(IdempotencyKey)


def invalid():
    return AppError("validation_error", 422, "Invalid knowledge source")


def conflict():
    return AppError("idempotency_conflict", 409, "Idempotency key conflicts with input")


def receipt(row):
    return ImportReceipt(
        import_id=row.id, job_id=row.job_id, target_generation=row.target_generation
    )


class KnowledgeService:
    def __init__(self, factory, policy, *, chunker=None, reader=None, index=None):
        self.factory, self.policy = factory, policy
        self.access = KnowledgeAccess(policy)
        self.chunker, self.reader, self.index = chunker, reader, index

    async def _admin(self, db, actor):
        # Called only after KnowledgeAccess.lock has refreshed/locked this User.
        user = await db.get(User, actor.id)
        if actor.role != "admin" or user.role != "admin":
            raise AppError("forbidden", 403, "Administrator access required")

    async def _capacity(self, db, repository_id):
        # ponytail: serialize global logical capacity; shard only with measured contention.
        await db.execute(
            insert(KnowledgeCapacity).values(id=1).on_conflict_do_nothing()
        )
        capacity = await db.get(KnowledgeCapacity, 1, with_for_update=True)
        await db.get(RepositoryCapacity, repository_id, with_for_update=True)
        return capacity

    async def _initialize_repository_capacity(self, db, repository_id):
        # Global capacity is already locked. Create a missing row only after auth:
        # its repository FK must not take a key-share lock ahead of User/Installation.
        await db.execute(
            insert(RepositoryCapacity)
            .values(repository_id=repository_id)
            .on_conflict_do_nothing()
        )

    async def authorize_ingress(self, actor, repository_id):
        """Fresh admin/base access that must precede any raw stream consumption."""
        if actor.role != "admin":
            raise AppError("forbidden", 403, "Administrator access required")
        return await self.access.verify(actor, (repository_id,), fresh=True)

    async def stage_upload(
        self, actor, repository_id, filename, body, *, document_id=None
    ):
        stage, _ = await self._stage(
            actor,
            repository_id,
            filename,
            body,
            document_id=document_id,
            idempotency_key=None,
        )
        return stage.id

    async def stage_upload_http(
        self, actor, repository_id, filename, body, *, document_id, idempotency_key
    ):
        """Keyed staging exchange: the same key and bytes reuse one durable stage."""
        stage, _ = await self._stage(
            actor,
            repository_id,
            filename,
            body,
            document_id=document_id,
            idempotency_key=idempotency_key,
        )
        return StageReceipt(staging_id=stage.id, expires_at=stage.expires_at)

    async def _stage(
        self, actor, repository_id, filename, body, *, document_id, idempotency_key
    ):
        if actor.role != "admin":
            raise AppError("forbidden", 403, "Administrator access required")
        try:
            upload_filename(filename)
            if not isinstance(body, bytes) or len(body) > 1048576 or b"\0" in body:
                raise ValueError
            body.decode("utf-8")
            key = (
                KEY.validate_python(idempotency_key)
                if idempotency_key is not None
                else None
            )
        except (ValueError, UnicodeError, ValidationError):
            raise invalid() from None
        grant = await self.access.verify(actor, (repository_id,), fresh=True)
        digest = hashlib.sha256(body).hexdigest()
        purpose = "replace" if document_id is not None else "new"
        request_hash = hashlib.sha256(
            json.dumps(
                {
                    "repository_id": str(repository_id),
                    "document_id": str(document_id) if document_id else None,
                    "purpose": purpose,
                    "filename": filename,
                    "sha256": digest,
                    "bytes": len(body),
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        async with transaction(self.factory) as db:
            capacity = await self._capacity(db, repository_id)
            await self.access.lock(db, grant)
            await self._admin(db, actor)
            await self._initialize_repository_capacity(db, repository_id)
            if key is not None:
                existing = await db.scalar(
                    select(UploadStage)
                    .where(
                        UploadStage.actor_id == actor.id,
                        UploadStage.repository_id == repository_id,
                        UploadStage.idempotency_key == key,
                    )
                    .with_for_update()
                )
                if existing is not None:
                    # Compare the exact originals, not only the digest: a key that
                    # is reused for different bytes is a conflict, never a replay.
                    if (
                        existing.request_hash,
                        existing.purpose,
                        existing.filename,
                        existing.document_id,
                        existing.body_bytes,
                    ) != (request_hash, purpose, filename, document_id, len(body)):
                        raise conflict()
                    now = await db.scalar(select(func.clock_timestamp()))
                    if existing.expires_at <= now:
                        # Replay never renews the original 24h window.
                        raise AppError("stage_expired", 409, "Upload stage expired")
                    return existing, False
            if document_id is None:
                document = Document(
                    repository_id=repository_id,
                    source_repository_id=repository_id,
                    kind="upload",
                    attributes={"filename": filename},
                )
                db.add(document)
                await db.flush()
            else:
                document = await db.get(Document, document_id, with_for_update=True)
                if (
                    document is None
                    or document.repository_id != repository_id
                    or document.kind != "upload"
                    or document.deleted
                ):
                    raise not_found()
            revision = await db.scalar(
                select(DocumentRevision)
                .where(
                    DocumentRevision.document_id == document.id,
                    DocumentRevision.source_version == digest,
                    DocumentRevision.revision_hash == digest,
                )
                .with_for_update()
            )
            if revision is None:
                if (
                    capacity.body_bytes + capacity.reserved_bytes + len(body)
                    > 134217728
                ):
                    raise AppError(
                        "storage_exhausted", 409, "Knowledge storage exhausted"
                    )
                revision = DocumentRevision(
                    document_id=document.id,
                    revision_hash=digest,
                    source_version=digest,
                    body=body,
                    body_bytes=len(body),
                )
                db.add(revision)
                capacity.body_bytes += len(body)
                await db.flush()
                db.add(
                    RevisionDependency(
                        revision_id=revision.id, repository_id=repository_id
                    )
                )
            elif revision.body is None:
                if revision.availability != "expired" or revision.body_bytes != len(
                    body
                ):
                    # A staged credential never resurrects deleted or corrupt sources.
                    raise not_found()
                if (
                    capacity.body_bytes + capacity.reserved_bytes + len(body)
                    > 134217728
                ):
                    raise AppError(
                        "storage_exhausted", 409, "Knowledge storage exhausted"
                    )
                # Re-staging identical bytes for a live document restores the
                # reclaimed canonical body; the revision identity is unchanged.
                revision.body = body
                revision.availability = "available"
                revision.searchable = False
                capacity.body_bytes += len(body)
            elif revision.availability != "available":
                raise not_found()
            now = await db.scalar(select(func.clock_timestamp()))
            stage = UploadStage(
                actor_id=actor.id,
                revision_id=revision.id,
                filename=filename,
                created_at=now,
                expires_at=now + timedelta(hours=24),
                repository_id=repository_id if key is not None else None,
                purpose=purpose if key is not None else None,
                idempotency_key=key,
                request_hash=request_hash if key is not None else None,
                # The caller's replacement selector only: an auto-created upload
                # document identity must not be mistaken for a client input.
                document_id=document_id if key is not None else None,
                body_bytes=len(body) if key is not None else None,
            )
            db.add(stage)
            await db.flush()
            return stage, True

    async def _existing(self, db, actor, request, key):
        return await db.scalar(
            select(KnowledgeImport).where(
                KnowledgeImport.actor_id == actor.id,
                KnowledgeImport.repository_id == request.repository_id,
                KnowledgeImport.idempotency_key == key,
            )
        )

    async def request_import(self, actor, request, idempotency_key):
        if actor.role != "admin":
            raise AppError("forbidden", 403, "Administrator access required")
        try:
            # Revalidate even DTO inputs: model_copy/model_construct are not trust boundaries.
            data = (
                request.model_dump(mode="json")
                if hasattr(request, "model_dump")
                else request
            )
            request = REQUEST.validate_python(data)
            key = KEY.validate_python(idempotency_key)
        except (ValidationError, ValueError):
            raise invalid() from None
        payload = request.model_dump(mode="json")
        try:
            metadata_bytes(payload, 8192)
        except ValueError:
            raise invalid() from None
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        version = None
        observed = None
        async with self.factory() as db:
            previous = await self._existing(db, actor, request, key)
            if previous is not None:
                dependencies = tuple(
                    await db.scalars(
                        select(ImportRepositoryEpoch.repository_id).where(
                            ImportRepositoryEpoch.import_id == previous.id
                        )
                    )
                )
            else:
                corpus = await db.get(KnowledgeCorpus, request.repository_id)
                observed = (
                    (
                        corpus.target_generation,
                        corpus.target_workspace_id,
                        corpus.target_sha,
                        corpus.path_scopes,
                    )
                    if corpus
                    else (0, None, None, {})
                )
                target_id = (
                    request.workspace_id if request.kind == "workspace" else observed[1]
                )
                version = (
                    await db.get(WorkspaceVersion, target_id) if target_id else None
                )
                if target_id and (
                    version is None
                    or version.repository_id != request.repository_id
                    or version.status != "ready"
                ):
                    raise not_found()
                from uuid import UUID

                dependencies = (
                    {UUID(scope["repository_id"]) for scope in version.scopes}
                    if version
                    else {request.repository_id}
                )
                if (
                    version
                    and not {version.repository_id, version.source_repository_id}
                    <= dependencies
                ):
                    raise not_found()
                if request.kind == "delete":
                    affected = await db.get(Document, request.document_id)
                    if (
                        affected is None
                        or affected.repository_id != request.repository_id
                        or affected.deleted
                    ):
                        raise not_found()
                    dependencies.add(affected.source_repository_id)
                    dependencies.update(
                        await db.scalars(
                            select(RevisionDependency.repository_id)
                            .join(
                                DocumentRevision,
                                DocumentRevision.id == RevisionDependency.revision_id,
                            )
                            .where(DocumentRevision.document_id == affected.id)
                        )
                    )
        grant = await self.access.verify(actor, dependencies, fresh=True)
        async with transaction(self.factory) as db:
            await self._capacity(db, request.repository_id)
            await self.access.lock(db, grant)
            await self._admin(db, actor)
            await self._initialize_repository_capacity(db, request.repository_id)
            existing = await self._existing(db, actor, request, key)
            if existing is not None:
                if existing.request_hash != digest:
                    raise conflict()
                await self._original_epochs(db, existing, grant)
                return receipt(existing)
            source_id, sha, workspace_id = request.repository_id, None, None
            if version is not None:
                current = await db.scalar(
                    select(WorkspaceVersion)
                    .where(WorkspaceVersion.id == version.id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
                if (
                    version is None
                    or current is None
                    or current.status != "ready"
                    or (
                        current.scopes,
                        current.commit_sha,
                        current.source_repository_id,
                    )
                    != (
                        version.scopes,
                        version.commit_sha,
                        version.source_repository_id,
                    )
                ):
                    raise not_found()
                epochs = {str(e.repository_id): e for e in grant.repository_epochs}
                for scope in current.scopes:
                    epoch = epochs.get(scope["repository_id"])
                    if epoch is None or (
                        epoch.installation_id,
                        epoch.installation_version,
                        epoch.repository_version,
                    ) != (
                        scope["installation_id"],
                        scope["installation_version"],
                        scope["repository_version"],
                    ):
                        raise not_found()
                if request.kind == "workspace":
                    source_id, sha, workspace_id = (
                        current.source_repository_id,
                        current.commit_sha,
                        current.id,
                    )
            await db.execute(
                insert(KnowledgeCorpus)
                .values(repository_id=request.repository_id)
                .on_conflict_do_nothing()
            )
            corpus = await db.get(
                KnowledgeCorpus, request.repository_id, with_for_update=True
            )
            if (
                corpus.target_generation,
                corpus.target_workspace_id,
                corpus.target_sha,
                corpus.path_scopes,
            ) != observed:
                raise conflict()
            stage = document = revision = None
            if request.kind == "upload":
                stage = await db.get(
                    UploadStage, request.staging_id, with_for_update=True
                )
                if (
                    stage is None
                    or stage.actor_id != actor.id
                    or stage.filename != request.filename
                    or stage.consumed_import_id is not None
                ):
                    raise not_found()
                # Read only the authorization reference before loading canonical bytes.
                document_id = await db.scalar(
                    select(DocumentRevision.document_id).where(
                        DocumentRevision.id == stage.revision_id
                    )
                )
                document = await db.get(Document, document_id, with_for_update=True)
                if (
                    document is None
                    or document.repository_id != request.repository_id
                    or document.kind != "upload"
                    or document.deleted
                    or (
                        request.document_id is not None
                        and request.document_id != document.id
                    )
                ):
                    raise not_found()
                revision = await db.get(
                    DocumentRevision, stage.revision_id, with_for_update=True
                )
                if (
                    revision.document_id != document.id
                    or revision.availability != "available"
                    or revision.body is None
                ):
                    raise not_found()
                await db.execute(
                    update(DocumentRevision)
                    .where(DocumentRevision.document_id == document.id)
                    .values(searchable=False)
                )
                document.accepted_revision_id = revision.id
            elif request.kind == "delete":
                document = await db.get(
                    Document,
                    request.document_id,
                    with_for_update=True,
                    populate_existing=True,
                )
                if (
                    document is None
                    or document.deleted
                    or document.repository_id != request.repository_id
                ):
                    raise not_found()
                document.deleted, document.accepted_revision_id = True, None
                await db.execute(
                    update(DocumentRevision)
                    .where(DocumentRevision.document_id == document.id)
                    .values(searchable=False, availability="deleted")
                )
            else:
                corpus.target_workspace_id, corpus.target_sha = workspace_id, sha
                corpus.path_scopes = {
                    "document_paths": list(request.document_paths),
                    "code_paths": list(request.code_paths),
                }
            now = await db.scalar(select(func.clock_timestamp()))
            if stage is not None and stage.expires_at <= now:
                raise not_found()
            corpus.target_generation += 1
            identity = uuid4()
            job = Job(
                kind="knowledge.import",
                repository_id=request.repository_id,
                requested_by=actor.id,
                input=KnowledgeImportInput(
                    import_id=identity, target_generation=corpus.target_generation
                ).model_dump(mode="json"),
                input_hash=digest,
                idempotency_key=identity.hex,
                max_attempts=3,
            )
            db.add(job)
            await db.flush()
            imported = KnowledgeImport(
                id=identity,
                repository_id=request.repository_id,
                source_repository_id=source_id,
                actor_id=actor.id,
                actor_github_id=actor.github_id,
                actor_auth_version=actor.auth_version,
                kind=request.kind,
                idempotency_key=key,
                request_hash=digest,
                request=payload,
                target_generation=corpus.target_generation,
                workspace_id=workspace_id,
                sha=sha,
                job_id=job.id,
                created_at=now,
                deadline=now + timedelta(hours=24),
                composition_version=1,
                target_workspace_id=corpus.target_workspace_id,
                target_sha=corpus.target_sha,
                target_path_scopes=dict(corpus.path_scopes),
                workspace_done=corpus.target_workspace_id is None,
                cursor=1 if stage else 0,
            )
            db.add(imported)
            await db.flush()
            for epoch in grant.repository_epochs:
                db.add(ImportRepositoryEpoch(import_id=identity, **asdict(epoch)))
            if stage is not None:
                stage.consumed_import_id = identity
                db.add(
                    ImportMember(
                        import_id=identity,
                        source_key=str(document.id),
                        ordinal=0,
                        revision_id=revision.id,
                    )
                )
            await append_event(db, job, "queued")
            await write_audit(
                db,
                AuditEntry(
                    actor_id=actor.id,
                    action="job.submitted",
                    resource_type="job",
                    resource_id=job.id,
                    job_id=job.id,
                    outcome="success",
                ),
            )
            final_now = await db.scalar(select(func.clock_timestamp()))
            if final_now >= imported.deadline or (
                stage is not None and final_now >= stage.expires_at
            ):
                raise not_found()
            return receipt(imported)

    async def _original_epochs(self, db, imported, grant):
        if (
            imported.actor_id,
            imported.actor_github_id,
            imported.actor_auth_version,
        ) != (grant.actor.id, grant.actor.github_id, grant.actor.auth_version):
            raise not_found()
        expected = {
            row.repository_id: row
            for row in await db.scalars(
                select(ImportRepositoryEpoch).where(
                    ImportRepositoryEpoch.import_id == imported.id
                )
            )
        }
        if set(expected) != {e.repository_id for e in grant.repository_epochs}:
            raise not_found()
        for epoch in grant.repository_epochs:
            if any(
                getattr(expected[epoch.repository_id], field) != value
                for field, value in asdict(epoch).items()
            ):
                raise not_found()

    async def import_batch(self, lease):
        from devflow.knowledge.ingestion import import_batch

        return await import_batch(self, lease)

    async def completed_chunks(self, import_id):
        from devflow.knowledge.ingestion import completed_chunks

        async for chunk in completed_chunks(self, import_id):
            yield chunk
