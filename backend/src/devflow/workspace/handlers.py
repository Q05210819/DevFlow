"""Installation-only prepare path, with one owned thread through physical drain."""

import asyncio
import threading
import time

from devflow.errors import AppError
from devflow.jobs.queue import Queue
from devflow.sync.scope import transaction
from devflow.workspace.git import GitCache
from devflow.workspace.guards import reserve_guard, revoke_reservation
from devflow.workspace.process import GitRunner, WorkspaceProcessError, check_storage
from devflow.workspace.schemas import SELECTION, WorkspaceResult
from devflow.workspace.scope import denied
from devflow.workspace.service import job_version
from devflow.workspace.sources import resolve, unavailable


async def owned_thread(function, cancel):
    work = asyncio.create_task(asyncio.to_thread(function))
    try:
        return await asyncio.shield(work)
    except asyncio.CancelledError:
        cancel.set()
        # Repeated cancellation cannot abandon the owner while Git may still live.
        while not work.done():
            try:
                await asyncio.shield(work)
            except asyncio.CancelledError:
                continue
            except Exception:  # noqa: BLE001 - original cancellation is preserved
                break
        if work.done() and not work.cancelled():
            work.exception()
        raise


async def prepare(service, lease):
    deadline = time.monotonic() + 600
    guard = None
    cancel = threading.Event()
    cache = service.cache or GitCache(
        service.settings.workspace_cache_root, GitRunner(service.settings)
    )

    async def fenced():
        async with transaction(service.factory) as db:
            version = await job_version(db, lease.job_id, service.settings)
            job, _ = await Queue(service.factory)._owned(db, lease)
            if (
                job is None
                or job.input.get("workspace_id") != str(version.id)
                or version.status != "queued"
            ):
                raise denied()
            return version

    async def scope_for(number):
        for scope in version.scopes:
            if scope["github_repository_id"] == number:
                return scope
        raise unavailable()

    async def token_for(scope, permissions):
        await fenced()
        return await service.tokens.get(
            scope["installation_id"],
            scope["github_repository_id"],
            permissions,
            auth_version=(scope["installation_version"], scope["repository_version"]),
            before_request=fenced,
        )

    async def read(scope, endpoint, **kwargs):
        permission = (
            "actions"
            if endpoint == "attempt"
            else "pull_requests"
            if endpoint == "pull"
            else "contents"
        )
        token = await token_for(scope, {permission: "read"})
        return (
            await service.data.read(token, scope["full_name"], endpoint, **kwargs)
        ).item

    try:
        async with asyncio.timeout_at(asyncio.get_running_loop().time() + 600):
            version = await fenced()
            base = next(
                scope
                for scope in version.scopes
                if scope["repository_id"] == str(version.repository_id)
            )
            sha, tree, proof, _ = await resolve(
                read, scope_for, base, SELECTION.validate_python(version.selection)
            )
            if (sha, tree, proof.model_dump(mode="json")) != (
                version.commit_sha,
                version.tree_oid,
                version.source,
            ):
                raise AppError(
                    "version_unavailable"
                    if version.selection["kind"] == "ci"
                    else "version_changed",
                    409,
                    "Selected version changed",
                )
            token = await token_for(base, {"contents": "read"})
            # Worker-only creation, before reservation or supervisor activation.
            await owned_thread(
                lambda: cache.prepare_lock(version.cache_repository_id), cancel
            )
            guard = await reserve_guard(
                service.factory,
                service.settings,
                version.id,
                kind="write",
                deadline=deadline,
                lease=lease,
                lock_path=cache.lock_path(version.cache_repository_id),
            )
            limits = (
                service.settings.workspace_repository_bytes,
                service.settings.workspace_total_bytes,
            )
            # The same limits reach the supervisor's active-process monitor.
            cache.runner.repository_bytes, cache.runner.total_bytes = limits
            await owned_thread(
                lambda: check_storage(
                    cache.root, version.cache_repository_id, limits, deadline
                ),
                cancel,
            )
            attempt_key = guard.operation_id
            commit = await owned_thread(
                lambda: cache.fetch(
                    version.cache_repository_id,
                    base["full_name"],
                    proof.ref,
                    token,
                    attempt_key,
                    guard=guard,
                    deadline=deadline,
                    cancel=cancel,
                    budget_check=lambda _repo: check_storage(
                        cache.root, version.cache_repository_id, limits, deadline
                    ),
                ),
                cancel,
            )
            if not await service.publish(lease, commit, guard=guard):
                raise denied()
            return WorkspaceResult(
                workspace_id=version.id, commit_sha=version.commit_sha
            )
    except WorkspaceProcessError as error:
        code = (
            "attempt_timeout"
            if str(error) == "timeout"
            else "cleanup_required"
            if str(error) == "cleanup_required"
            else "storage_exhausted"
            if str(error) == "storage_exhausted"
            else "git_failed"
        )
        if version.selection["kind"] == "ci" and code == "git_failed":
            code = "version_unavailable"
        raise AppError(code, 503, "Workspace preparation unavailable") from None
    finally:
        cancel.set()
        if guard is not None:
            # A rejected/missing activation may still leave a reservation. The
            # CAS can revoke only never-activated work, never its surviving tree.
            await asyncio.shield(revoke_reservation(service.factory, guard))
