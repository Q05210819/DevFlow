"""One durable admission record; expiry revokes authority, never proves death."""

import asyncio
import math
import re
import time
from dataclasses import asdict
from datetime import timedelta
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from sqlalchemy import func, select, update
from sqlalchemy.engine import make_url

from devflow.errors import AppError
from devflow.jobs.queue import Queue
from devflow.sync.scope import transaction
from devflow.workspace.models import WorkspaceProcessGuard, WorkspaceVersion
from devflow.workspace.process import GuardRecord, _identity
from devflow.workspace.scope import denied, lock_maintenance, lock_scopes

RUNTIME_ID = uuid4()
UNRESOLVED = ("reserved", "active", "cleanup_required")


async def admission_lock(db):
    await db.execute(
        select(
            func.pg_advisory_xact_lock(
                func.hashtextextended("devflow:workspace:admission", 0)
            )
        )
    )


async def revoke_expired_reservations(db):
    # CAS excludes activated rows, even when their Job/request has expired.
    await db.execute(
        update(WorkspaceProcessGuard)
        .where(
            WorkspaceProcessGuard.state == "reserved",
            WorkspaceProcessGuard.expires_at <= func.clock_timestamp(),
        )
        .values(
            state="drained", proof="never_activated", drained_at=func.clock_timestamp()
        )
    )


async def reserve_guard(
    factory,
    settings,
    workspace_id,
    *,
    kind,
    deadline,
    lock_path,
    lease=None,
    actor=None,
):
    remaining = deadline - time.monotonic()
    if (
        kind not in {"read", "write", "gc"}
        or not math.isfinite(deadline)
        or not 0 < remaining <= (5 if kind == "read" else 600)
    ):
        raise AppError("attempt_timeout", 503, "Workspace deadline exceeded")
    async with asyncio.timeout(remaining), transaction(factory) as db:
        await admission_lock(db)
        await revoke_expired_reservations(db)
        version = await db.get(WorkspaceVersion, workspace_id)
        if version is None:
            raise denied("workspace_not_found")
        if kind == "gc":
            if actor is not None or lease is not None:
                raise denied()
            await lock_maintenance(db, version)
        else:
            await lock_scopes(db, version.scopes, settings, actor=actor)
        if kind == "write":
            job, _ = await Queue(factory)._owned(db, lease)
            if (
                job is None
                or job.id != version.job_id
                or job.kind != "repository.workspace"
            ):
                raise denied()
        elif kind == "read" and actor is None:
            raise denied()
        version = await db.scalar(
            select(WorkspaceVersion)
            .where(WorkspaceVersion.id == workspace_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if kind == "read" and version.status != "ready":
            raise denied("version_unavailable")
        unresolved = list(
            await db.scalars(
                select(WorkspaceProcessGuard).where(
                    WorkspaceProcessGuard.state.in_(UNRESOLVED)
                )
            )
        )
        if kind == "read":
            busy = sum(row.kind == "read" for row in unresolved) >= 4 or any(
                row.kind != "read" and row.repository_id == version.cache_repository_id
                for row in unresolved
            )
        else:
            busy = any(
                row.kind != "read" or row.repository_id == version.cache_repository_id
                for row in unresolved
            )
        if busy:
            raise AppError(
                "workspace_busy", 503, "Workspace operation capacity is occupied"
            )
        path = Path(lock_path)
        if (
            not path.is_absolute()
            or path.name != f"{version.cache_repository_id}.lock"
            or path.parent.name != ".locks"
        ):
            raise AppError("validation_error", 400, "Invalid workspace lock")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AppError("attempt_timeout", 503, "Workspace deadline exceeded")
        now = await db.scalar(select(func.clock_timestamp()))
        if kind == "write" and job.lease_expires_at <= now:
            raise denied()
        if kind == "read":
            version.last_used_at = now
        guard = GuardRecord(
            uuid4().hex,
            uuid4().hex,
            version.cache_repository_id,
            kind,
            path,
            settings.database_url.get_secret_value(),
        )
        db.add(
            WorkspaceProcessGuard(
                operation_id=guard.operation_id,
                nonce=guard.nonce,
                repository_id=guard.repository_id,
                workspace_id=version.id,
                kind=kind,
                runtime_id=RUNTIME_ID,
                lock_path=str(path),
                deadline=deadline,
                expires_at=now + timedelta(seconds=remaining),
                scopes=version.scopes,
                actor={
                    "id": str(actor.id),
                    "github_id": actor.github_id,
                    "auth_version": actor.auth_version,
                }
                if actor
                else None,
                lease={
                    key: str(value) if isinstance(value, UUID) else value
                    for key, value in asdict(lease).items()
                }
                if lease
                else None,
            )
        )
    return guard


async def revoke_reservation(factory, guard):
    """Used on parent cancellation; never converts an active row to proof."""
    async with transaction(factory) as db:
        await db.execute(
            update(WorkspaceProcessGuard)
            .where(
                WorkspaceProcessGuard.operation_id == guard.operation_id,
                WorkspaceProcessGuard.nonce == guard.nonce,
                WorkspaceProcessGuard.state == "reserved",
            )
            .values(
                state="drained",
                proof="never_activated",
                drained_at=func.clock_timestamp(),
            )
        )


def activate_guard(guard_dict, identity_dict):
    return _bridge(guard_dict, identity_dict, draining=False)


def drain_guard(guard_dict):
    return _bridge(guard_dict, None, draining=True)


def _bridge(guard, identity, *, draining):
    try:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            return False
        for name in ("operation_id", "nonce"):
            if not isinstance(guard[name], str) or not re.fullmatch(
                r"[0-9a-f]{32}", guard[name]
            ):
                return False
        deadline = guard["cleanup_deadline" if draining else "deadline"]
        original = guard["deadline"]
        if (
            not math.isfinite(deadline)
            or not math.isfinite(original)
            or deadline <= time.monotonic()
            or deadline > original + (2 if draining else 0)
        ):
            return False
        current = _identity("Local\\DevFlow-" + guard["operation_id"])
        if not draining and identity != current:
            return False
        return asyncio.run(
            _callback(guard, current, deadline, draining),
            loop_factory=asyncio.SelectorEventLoop,
        )
    except Exception:  # noqa: BLE001 - no secrets or raw DB errors leave this boundary
        return False


async def _one(connection, sql, values=()):
    cursor = await connection.execute(sql, values)
    return await cursor.fetchone()


async def _callback(guard, identity, deadline, draining):
    remaining = deadline - time.monotonic()
    async with asyncio.timeout(remaining):
        url = make_url(guard["database_url"])
        if url.drivername != "postgresql+psycopg":
            return False
        dsn = url.set(drivername="postgresql").render_as_string(hide_password=False)
        async with await psycopg.AsyncConnection.connect(
            dsn, row_factory=dict_row
        ) as connection:
            millis = max(1, int((deadline - time.monotonic()) * 1000))
            await connection.execute(
                "SELECT set_config('statement_timeout', %s, true), set_config('lock_timeout', %s, true)",
                (str(millis), str(millis)),
            )
            row = await _one(
                connection,
                "SELECT * FROM workspace_process_guards WHERE operation_id=%s",
                (guard["operation_id"],),
            )
            if row is None or any(
                row[key] != guard[key]
                for key in ("nonce", "repository_id", "kind", "lock_path", "deadline")
            ):
                return False
            if not draining:
                maintenance = row["kind"] == "gc"
                if maintenance and (row["actor"] or row["lease"]):
                    return False
                if row["actor"]:
                    actor = row["actor"]
                    user = await _one(
                        connection,
                        "SELECT enabled,auth_version,github_id FROM users WHERE id=%s FOR UPDATE",
                        (actor["id"],),
                    )
                    if (
                        not user
                        or not user["enabled"]
                        or any(
                            user[key] != actor[key]
                            for key in ("auth_version", "github_id")
                        )
                    ):
                        return False
                installations = {}
                maintenance_installations = []
                if maintenance:
                    for scope in row["scopes"]:
                        owned = await _one(
                            connection,
                            "SELECT installation_id FROM repositories WHERE id=%s",
                            (scope["repository_id"],),
                        )
                        if not owned:
                            return False
                        maintenance_installations.append(owned["installation_id"])
                for installation_id in sorted(
                    set(maintenance_installations)
                    if maintenance
                    else {scope["installation_id"] for scope in row["scopes"]}
                ):
                    installations[installation_id] = await _one(
                        connection,
                        "SELECT * FROM github_installations WHERE installation_id=%s FOR UPDATE",
                        (installation_id,),
                    )
                for scope in sorted(
                    row["scopes"], key=lambda item: item["repository_id"]
                ):
                    repo = await _one(
                        connection,
                        "SELECT * FROM repositories WHERE id=%s FOR UPDATE",
                        (scope["repository_id"],),
                    )
                    installation = installations.get(
                        repo["installation_id"]
                        if maintenance and repo
                        else scope["installation_id"]
                    )
                    if (
                        not repo
                        or not installation
                        or repo["github_repository_id"] != scope["github_repository_id"]
                        or not maintenance
                        and (
                            not repo["enabled"]
                            or not repo["available"]
                            or repo["needs_verification"]
                            or not installation["available"]
                            or installation["needs_verification"]
                            or repo["installation_id"] != scope["installation_id"]
                            or repo["auth_version"] != scope["repository_version"]
                            or installation["auth_version"]
                            != scope["installation_version"]
                            or repo["full_name"] != scope["full_name"]
                        )
                    ):
                        return False
                if row["lease"]:
                    lease = row["lease"]
                    job = await _one(
                        connection,
                        "SELECT *,lease_expires_at > clock_timestamp() AS valid FROM jobs WHERE id=%s FOR UPDATE",
                        (lease["job_id"],),
                    )
                    if (
                        not job
                        or job["kind"] != "repository.workspace"
                        or job["status"] != "running"
                        or not job["valid"]
                        or str(job["lease_owner"]) != lease["owner"]
                        or job["lease_generation"] != lease["generation"]
                        or job["attempt"] != lease["attempt"]
                    ):
                        return False
                version = await _one(
                    connection,
                    "SELECT * FROM workspace_versions WHERE id=%s FOR UPDATE",
                    (row["workspace_id"],),
                )
                if (
                    not version
                    or version["scopes"] != row["scopes"]
                    or row["kind"] == "read"
                    and version["status"] != "ready"
                    or row["kind"] == "write"
                    and (
                        not row["lease"]
                        or str(version["job_id"]) != row["lease"]["job_id"]
                    )
                    or maintenance
                    and (
                        version["cache_repository_id"] != row["repository_id"]
                        or not any(
                            scope["repository_id"] == str(version["repository_id"])
                            and scope["github_repository_id"] == row["repository_id"]
                            for scope in row["scopes"]
                        )
                    )
                ):
                    return False
            locked = await _one(
                connection,
                "SELECT *,expires_at > clock_timestamp() AS valid FROM workspace_process_guards WHERE operation_id=%s FOR UPDATE",
                (guard["operation_id"],),
            )
            if (
                not locked
                or locked["nonce"] != guard["nonce"]
                or time.monotonic() >= deadline
            ):
                return False
            if draining:
                if locked["identity"] != identity:
                    return False
                if locked["state"] == "drained":
                    return locked["proof"] == "physical"
                if locked["state"] not in {"active", "cleanup_required"}:
                    return False
                await connection.execute(
                    "UPDATE workspace_process_guards SET state='drained',proof='physical',drained_at=clock_timestamp() WHERE operation_id=%s AND nonce=%s",
                    (guard["operation_id"], guard["nonce"]),
                )
            else:
                if locked["state"] != "reserved" or not locked["valid"]:
                    return False
                if locked["lease"]:
                    current = await _one(
                        connection,
                        "SELECT lease_expires_at > clock_timestamp() AS valid FROM jobs WHERE id=%s",
                        (locked["lease"]["job_id"],),
                    )
                    if not current or not current["valid"]:
                        return False
                await connection.execute(
                    "UPDATE workspace_process_guards SET state='active',identity=%s WHERE operation_id=%s AND nonce=%s",
                    (Jsonb(identity), guard["operation_id"], guard["nonce"]),
                )
            await connection.commit()
            return True
