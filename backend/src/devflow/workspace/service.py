"""User-authorized immutable intent; background publication is separately fenced."""

import asyncio
import hashlib
import json
from datetime import datetime
from uuid import UUID, uuid4

from pydantic import TypeAdapter, ValidationError
from sqlalchemy import exists, func, select, tuple_

from devflow.audit.service import AuditEntry, write_audit
from devflow.errors import AppError
from devflow.github.data import GitHubDataClient
from devflow.github.installations import InstallationTokens
from devflow.jobs.models import Job
from devflow.jobs.queue import Queue
from devflow.jobs.schemas import IdempotencyKey
from devflow.jobs.service import append_event
from devflow.pagination import CursorCodec
from devflow.repositories.models import Repository
from devflow.security import read_secret_file
from devflow.sync.scope import transaction
from devflow.workspace.models import WorkspaceProcessGuard, WorkspaceVersion
from devflow.workspace.schemas import (
    SELECTION,
    WorkspacePage,
    WorkspaceResult,
    WorkspaceView,
)
from devflow.workspace.scope import capture, denied, lock_scopes, recheck
from devflow.workspace.sources import resolve, unavailable


async def job_version(db, job_id, settings):
    version = await db.scalar(
        select(WorkspaceVersion).where(WorkspaceVersion.job_id == job_id)
    )
    if version is None:
        raise denied()
    await lock_scopes(db, version.scopes, settings)
    return version


class WorkspaceService:
    def __init__(
        self, factory, policy, settings, *, data=None, tokens=None, cache=None
    ):
        self.factory, self.policy, self.settings = factory, policy, settings
        self.data = data or GitHubDataClient(policy.github)
        self.tokens = tokens or InstallationTokens(policy.github)
        self.cache = cache

    async def request(self, actor, repo, selection, key):
        try:
            parsed = SELECTION.validate_python(selection)
            key = TypeAdapter(IdempotencyKey).validate_python(key)
        except ValidationError:
            raise AppError(
                "validation_error", 422, "Invalid workspace selection"
            ) from None
        try:
            async with asyncio.timeout(30):
                return await self._request(actor, repo, parsed, key)
        except TimeoutError:
            raise AppError(
                "attempt_timeout", 503, "Workspace resolution timed out"
            ) from None

    async def _request(self, actor, repo, selection, key):
        base = await capture(self.policy, actor, repo, fresh=True)
        normalized = selection.model_dump(mode="json")
        digest = hashlib.sha256(
            json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        async with self.factory() as db:
            existing = await db.scalar(
                select(WorkspaceVersion).where(
                    WorkspaceVersion.repository_id == repo,
                    WorkspaceVersion.requested_by == actor.id,
                    WorkspaceVersion.idempotency_key == key,
                )
            )
        if existing:
            self._same_input(existing, digest)
            return await self._get(actor, repo, existing.id, fresh=True, base=base)
        scopes = {base["github_repository_id"]: base}
        token = await self.policy.credentials.get_user_token(actor)

        async def read(scope, endpoint, **kwargs):
            try:
                response = await self.data.read(
                    token, scope["full_name"], endpoint, installation=False, **kwargs
                )
                return response.item
            except AppError as error:
                if error.code == "reauth_required":
                    await self.policy.credentials.invalidate_user_token(actor, token)
                if scope["repository_id"] != base["repository_id"] and error.status in {
                    403,
                    404,
                }:
                    raise denied("fork_source_unavailable") from None
                raise

        async def scope_for(number):
            if number in scopes:
                return scopes[number]
            async with self.factory() as db:
                identity = await db.scalar(
                    select(Repository.id).where(
                        Repository.github_repository_id == number
                    )
                )
            if identity is None:
                raise denied("fork_source_unavailable")
            try:
                scopes[number] = await capture(self.policy, actor, identity, fresh=True)
            except AppError as error:
                if error.status in {403, 404}:
                    raise denied("fork_source_unavailable") from None
                raise
            return scopes[number]

        sha, tree, proof, source = await resolve(read, scope_for, base, selection)
        async with transaction(self.factory) as db:
            await lock_scopes(db, list(scopes.values()), self.settings, actor=actor)
            existing = await db.scalar(
                select(WorkspaceVersion).where(
                    WorkspaceVersion.repository_id == repo,
                    WorkspaceVersion.requested_by == actor.id,
                    WorkspaceVersion.idempotency_key == key,
                )
            )
            if existing:
                self._same_input(existing, digest)
                identity = existing.id
            else:
                identity = uuid4()
                job = Job(
                    kind="repository.workspace",
                    repository_id=repo,
                    requested_by=None,
                    input={"workspace_id": str(identity)},
                    input_hash=digest,
                    idempotency_key=identity.hex,
                    max_attempts=3,
                )
                db.add(job)
                await db.flush()
                db.add(
                    WorkspaceVersion(
                        id=identity,
                        repository_id=repo,
                        source_repository_id=UUID(source["repository_id"]),
                        cache_repository_id=base["github_repository_id"],
                        requested_by=actor.id,
                        idempotency_key=key,
                        input_hash=digest,
                        selection=normalized,
                        source=proof.model_dump(mode="json"),
                        scopes=list(scopes.values()),
                        commit_sha=sha,
                        tree_oid=tree,
                        job_id=job.id,
                    )
                )
                await append_event(db, job, "queued")
                await write_audit(
                    db,
                    AuditEntry(
                        actor_id=actor.id,
                        action="workspace.requested",
                        resource_type="workspace",
                        resource_id=identity,
                        job_id=job.id,
                        outcome="success",
                    ),
                )
        return await self._get(actor, repo, identity, fresh=False, base=base)

    @staticmethod
    def _same_input(version, digest):
        if version.input_hash != digest:
            raise AppError(
                "idempotency_conflict", 409, "Idempotency key conflicts with input"
            )

    async def _authorize_version(self, actor, version, base, *, fresh, known=None):
        known = {} if known is None else known
        known[base["repository_id"]] = base
        for expected in version.scopes:
            identity = expected["repository_id"]
            if identity not in known:
                try:
                    known[identity] = await capture(
                        self.policy, actor, UUID(identity), fresh=fresh
                    )
                except AppError as error:
                    if error.status in {403, 404}:
                        raise denied("fork_source_unavailable") from None
                    raise
            if known[identity] != expected:
                raise denied(
                    "workspace_not_found"
                    if identity == base["repository_id"]
                    else "fork_source_unavailable"
                )
        return version.scopes

    async def get(self, actor, repo, workspace_id):
        try:
            async with asyncio.timeout(30):
                return await self._get(actor, repo, workspace_id, fresh=False)
        except TimeoutError:
            raise AppError(
                "attempt_timeout", 503, "Workspace authorization timed out"
            ) from None

    async def _get(self, actor, repo, identity, *, fresh, base=None):
        base = base or await capture(self.policy, actor, repo, fresh=fresh)
        async with self.factory() as db:
            version = await db.scalar(
                select(WorkspaceVersion).where(
                    WorkspaceVersion.id == identity,
                    WorkspaceVersion.repository_id == repo,
                )
            )
        if version is None:
            raise denied("workspace_not_found")
        scopes = await self._authorize_version(actor, version, base, fresh=fresh)
        async with transaction(self.factory) as db:
            await lock_scopes(db, scopes, self.settings, actor=actor)
            version = await db.scalar(
                select(WorkspaceVersion)
                .where(WorkspaceVersion.id == identity)
                .with_for_update()
            )
            version.last_used_at = await db.scalar(select(func.clock_timestamp()))
            result = await self._view(db, version)
        await recheck(self.policy, actor, scopes)
        return result

    @staticmethod
    async def _view(db, version):
        job = await db.get(Job, version.job_id)
        status = version.status
        error = version.error
        if status == "queued":
            status = (
                job.status
                if job.status in {"queued", "running", "retry_wait", "failed"}
                else "unavailable"
            )
            error = job.error_code
        return WorkspaceView(
            id=version.id,
            repository_id=version.repository_id,
            source_repository_id=version.source_repository_id,
            commit_sha=version.commit_sha,
            tree_oid=version.tree_oid,
            source=version.source,
            status=status,
            error=error,
            created_at=version.created_at,
            last_used_at=version.last_used_at,
        )

    async def list(self, actor, repo, *, limit=20, cursor=None):
        try:
            async with asyncio.timeout(30):
                return await self._list(actor, repo, limit, cursor)
        except TimeoutError:
            raise AppError(
                "attempt_timeout", 503, "Workspace collection timed out"
            ) from None

    async def _list(self, actor, repo, limit, cursor):
        base = await capture(self.policy, actor, repo, fresh=False)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise AppError("validation_error", 422, "Invalid page limit")
        codec = CursorCodec(read_secret_file(self.settings.app_signing_key_file))
        purpose = f"workspace-list:v1:{repo}"
        query = select(WorkspaceVersion).where(WorkspaceVersion.repository_id == repo)
        if cursor:
            position = codec.decode_cursor(cursor, actor.id, purpose, limit)
            try:
                if set(position) != {"created_at", "id"}:
                    raise ValueError
                stamp, identity = (
                    datetime.fromisoformat(position["created_at"]),
                    UUID(position["id"]),
                )
                if stamp.tzinfo is None:
                    raise ValueError
            except (ValueError, TypeError, KeyError):
                raise AppError(
                    "invalid_cursor", 400, "Invalid workspace cursor"
                ) from None
            query = query.where(
                tuple_(WorkspaceVersion.created_at, WorkspaceVersion.id)
                < (stamp, identity)
            )
        async with self.factory() as db:
            versions = list(
                await db.scalars(
                    query.order_by(
                        WorkspaceVersion.created_at.desc(), WorkspaceVersion.id.desc()
                    ).limit(limit)
                )
            )
            more = bool(versions) and await db.scalar(
                select(
                    exists().where(
                        WorkspaceVersion.repository_id == repo,
                        tuple_(WorkspaceVersion.created_at, WorkspaceVersion.id)
                        < (versions[-1].created_at, versions[-1].id),
                    )
                )
            )
        known, missing, items, returned_scopes = {}, set(), [], {}
        unavailable_count = 0
        for version in versions:
            try:
                if any(scope["repository_id"] in missing for scope in version.scopes):
                    raise denied("fork_source_unavailable")
                await self._authorize_version(
                    actor, version, base, fresh=False, known=known
                )
                async with self.factory() as db:
                    items.append(await self._view(db, version))
                for scope in version.scopes:
                    returned_scopes[scope["repository_id"]] = scope
            except AppError as error:
                if error.code not in {"fork_source_unavailable", "workspace_not_found"}:
                    raise
                unavailable_count += 1
                missing.update(
                    scope["repository_id"]
                    for scope in version.scopes
                    if scope["repository_id"] != base["repository_id"]
                    and scope["repository_id"] not in known
                )
        await recheck(self.policy, actor, [base, *returned_scopes.values()])
        next_cursor = (
            codec.encode_cursor(
                actor.id,
                purpose,
                {
                    "created_at": versions[-1].created_at.isoformat(),
                    "id": str(versions[-1].id),
                },
                limit,
            )
            if more
            else None
        )
        return WorkspacePage(
            items=items,
            next_cursor=next_cursor,
            has_more=bool(more),
            unavailable_count=unavailable_count,
        )

    async def publish(self, lease, commit, *, guard=None):
        if guard is None:
            return False
        async with transaction(self.factory) as db:
            try:
                version = await job_version(db, lease.job_id, self.settings)
            except AppError:
                return False
            job, now = await Queue(self.factory)._owned(db, lease)
            if (
                job is None
                or job.kind != "repository.workspace"
                or job.input.get("workspace_id") != str(version.id)
            ):
                return False
            version = await db.scalar(
                select(WorkspaceVersion)
                .where(WorkspaceVersion.id == version.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            physical = await db.get(
                WorkspaceProcessGuard, guard.operation_id, with_for_update=True
            )
            now = await db.scalar(select(func.clock_timestamp()))
            if job.lease_expires_at <= now:
                return False
            if (
                physical is None
                or physical.kind != "write"
                or physical.repository_id != version.cache_repository_id
                or physical.scopes != version.scopes
                or physical.nonce != guard.nonce
                or physical.workspace_id != version.id
                or physical.state != "drained"
                or physical.proof != "physical"
                or physical.lease
                != {
                    "job_id": str(lease.job_id),
                    "owner": str(lease.owner),
                    "generation": lease.generation,
                    "attempt": lease.attempt,
                }
                or version.status != "queued"
            ):
                return False
            if (commit.sha, commit.tree_oid) != (version.commit_sha, version.tree_oid):
                raise AppError(
                    "version_unavailable"
                    if version.selection["kind"] == "ci"
                    else "version_changed",
                    409,
                    "Selected ref changed",
                )
            if commit.pin_ref != f"refs/devflow/pins/{guard.operation_id}":
                raise unavailable()
            version.pin_ref, version.status, version.error = (
                commit.pin_ref,
                "ready",
                None,
            )
            version.last_used_at = now
            job.status, job.error_code = "succeeded", None
            job.result = WorkspaceResult(
                workspace_id=version.id, commit_sha=version.commit_sha
            ).model_dump(mode="json")
            job.lease_owner = job.lease_expires_at = None
            job.updated_at = now
            await Queue._transition(db, job, "job.succeeded")
        return True

    async def prepare(self, lease):
        from devflow.workspace.handlers import prepare

        return await prepare(self, lease)
