"""Bounded maintenance; durable authority and physical death are separate fences."""

import asyncio
import json
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import exists, func, or_, select

from devflow.errors import AppError
from devflow.facts.models import Fact
from devflow.github.models import GitHubInstallation
from devflow.jobs.models import Job
from devflow.repositories.models import Repository
from devflow.sync.scope import authorized
from devflow.workspace.guards import reserve_guard, revoke_reservation
from devflow.workspace.handlers import owned_thread
from devflow.workspace.models import WorkspaceProcessGuard, WorkspaceVersion
from devflow.workspace.process import WorkspaceProcessError
from devflow.workspace.scope import lock_maintenance


@dataclass
class RetentionResult:
    examined: int = 0
    evicted: int = 0
    orphans: int = 0
    unavailable: int = 0
    deferred: int = 0


class WorkspaceRetention:
    def __init__(self, factory, cache, settings):
        self.factory, self.cache, self.settings = factory, cache, settings
        self._after = None
        self._candidate_after = {}

    async def invalidate(self, version):
        async with self.factory.begin() as db:
            await lock_maintenance(db, version)
            row = await db.get(WorkspaceVersion, version.id, with_for_update=True)
            if (
                row.status == "ready"
                and row.pin_ref == version.pin_ref
                and row.commit_sha == version.commit_sha
                and row.tree_oid == version.tree_oid
            ):
                row.status, row.error = "unavailable", "version_unavailable"

    @staticmethod
    def health_candidates(snapshot, default_branch):
        result = []
        if default_branch:
            result.append(({"kind": "branch", "name": default_branch}, None))
        for pull in sorted(snapshot.manifest.pulls, key=lambda item: item.number):
            for side in ("base", "head"):
                result.append(
                    (
                        {"kind": "pull", "number": pull.number, "side": side},
                        getattr(pull, side + "_sha"),
                    )
                )
        for action in snapshot.manifest.actions:
            result.append(
                (
                    {
                        "kind": "ci",
                        "run_id": action.source_id,
                        "attempt": action.attempt,
                    },
                    action.head_sha,
                )
            )
        return result

    @staticmethod
    def evidence_key(selection, sha):
        return json.dumps([selection, sha], sort_keys=True, separators=(",", ":"))

    async def _scope_available(self, db, version):
        for scope in version.scopes:
            repo = await db.get(Repository, UUID(scope["repository_id"]))
            installation = await db.scalar(
                select(GitHubInstallation).where(
                    GitHubInstallation.installation_id == scope["installation_id"],
                )
            )
            if (
                not repo
                or not installation
                or not authorized(installation, repo, self.settings)
                or repo.auth_version != scope["repository_version"]
                or installation.auth_version != scope["installation_version"]
                or repo.installation_id != scope["installation_id"]
            ):
                return False
        return True

    async def health_preflight(self, snapshot, repository_id, default_branch):
        """One three-second deadline; no waits for capacity or remote requests."""
        deadline = time.monotonic() + 3
        observations, checked, checks = {}, {}, 0
        try:
            async with asyncio.timeout(3):
                async with self.factory() as db:
                    versions = list(
                        await db.scalars(
                            select(WorkspaceVersion)
                            .where(
                                WorkspaceVersion.repository_id == repository_id,
                            )
                            .order_by(
                                WorkspaceVersion.created_at.desc(),
                                WorkspaceVersion.id.desc(),
                            )
                            .limit(1001)
                        )
                    )
                for selection, sha in self.health_candidates(snapshot, default_branch):
                    key = self.evidence_key(selection, sha)
                    version = next(
                        (
                            v
                            for v in versions
                            if v.selection == selection
                            and (sha is None or v.commit_sha == sha)
                        ),
                        None,
                    )
                    if version is None:
                        observations[key] = (
                            None,
                            "unknown" if len(versions) > 1000 else "unprepared",
                            None,
                            "verification_limit" if len(versions) > 1000 else None,
                        )
                        continue
                    async with self.factory() as db:
                        scope_ok = await self._scope_available(db, version)
                    if not scope_ok:
                        observations[key] = (version, "unavailable", None, None)
                        continue
                    if version.status != "ready" or not version.pin_ref:
                        observations[key] = (
                            version,
                            "unprepared"
                            if version.status == "queued"
                            else "unavailable",
                            None,
                            None,
                        )
                        continue
                    physical = (
                        version.cache_repository_id,
                        version.pin_ref,
                        version.commit_sha,
                        version.tree_oid,
                    )
                    if physical not in checked and checks < 16:
                        guard, cancel = None, threading.Event()
                        try:
                            guard = await reserve_guard(
                                self.factory,
                                self.settings,
                                version.id,
                                kind="gc",
                                deadline=deadline,
                                lock_path=self.cache.lock_path(
                                    version.cache_repository_id
                                ),
                            )
                            checks += 1

                            def inspect(version=version, guard=guard, cancel=cancel):
                                with self.cache.operation(
                                    guard, deadline=deadline, cancel=cancel
                                ):
                                    return self.cache.inspect(
                                        version.cache_repository_id,
                                        version.pin_ref,
                                        version.commit_sha,
                                        version.tree_oid,
                                    )

                            status = await owned_thread(inspect, cancel)
                            checked[physical] = (
                                status,
                                datetime.now(UTC) if status != "unknown" else None,
                                (
                                    "verification_limit"
                                    if time.monotonic() >= deadline
                                    else "verification_unavailable"
                                )
                                if status == "unknown"
                                else None,
                            )
                        except (AppError, WorkspaceProcessError):
                            checked[physical] = (
                                "unknown",
                                None,
                                "verification_limit"
                                if time.monotonic() >= deadline
                                else "verification_unavailable",
                            )
                        finally:
                            cancel.set()
                            if guard:
                                await asyncio.shield(
                                    revoke_reservation(self.factory, guard)
                                )
                    status, stamp, reason = checked.get(
                        physical, ("unknown", None, "verification_limit")
                    )
                    observations[key] = (version, status, stamp, reason)
                    if status == "unavailable":
                        await self.invalidate(version)
        except TimeoutError:
            pass
        return observations, checks

    async def health_evidence(self, db, snapshot, default_branch, prepared):
        from devflow.health.schemas import WorkspaceEvidence, WorkspaceSelectionSummary

        observations, checks = prepared
        entries, checked_count, unverified = [], 0, 0
        for selection, sha in self.health_candidates(snapshot, default_branch):
            observation = observations.get(self.evidence_key(selection, sha))
            version, state, stamp, reason = observation or (
                None,
                "unknown",
                None,
                "verification_limit",
            )
            if version is not None:
                row = await db.get(WorkspaceVersion, version.id, populate_existing=True)
                if (
                    not row
                    or row.pin_ref != version.pin_ref
                    or row.commit_sha != version.commit_sha
                ):
                    state, stamp, reason = "unknown", None, "version_changed"
                elif not await self._scope_available(db, row):
                    state, stamp, reason = "unavailable", None, "scope_unavailable"
                elif row.status in {"unavailable", "failed"}:
                    state, reason = "unavailable", "version_unavailable"
                elif row.status != "ready":
                    state, stamp, reason = "unprepared", None, "not_prepared"
                elif state == "available" and stamp is None:
                    state = "unknown"
            if state == "unknown":
                reason = reason or "verification_limit"
                unverified += 1
            elif state == "unprepared":
                reason = "not_prepared"
            elif state == "unavailable":
                reason = reason or "version_unavailable"
            if stamp is not None and state in {"available", "unavailable"}:
                checked_count += 1
            entries.append(
                WorkspaceEvidence(
                    selection=selection,
                    workspace_id=version.id if version else None,
                    source_repository_id=version.source_repository_id
                    if version
                    else None,
                    commit_sha=version.commit_sha if version else sha,
                    availability=state,
                    reason=reason,
                    checked_at=stamp,
                )
            )
        summary = WorkspaceSelectionSummary(
            total_candidates=len(entries),
            returned_count=len(entries),
            checked_pins=checks,
            checked_count=checked_count,
            unverified_count=unverified,
        )
        states = {entry.availability for entry in entries}
        status = (
            "available"
            if states == {"available"}
            else "partial"
            if "available" in states
            else "unknown"
            if not states or "unknown" in states
            else "unavailable"
        )
        return snapshot.model_copy(
            update={
                "workspace": status,
                "manifest": snapshot.manifest.model_copy(
                    update={
                        "workspace_versions": entries,
                        "workspace_selection": summary,
                    }
                ),
            }
        )

    async def _protected(self, db, version, repository):
        if await db.scalar(
            select(
                exists().where(
                    Fact.repository_id == version.repository_id,
                    Fact.kind == "pull",
                    Fact.unavailable.is_(False),
                    Fact.payload["state"].astext == "open",
                    or_(
                        Fact.payload["base_sha"].astext == version.commit_sha,
                        Fact.payload["head_sha"].astext == version.commit_sha,
                    ),
                )
            )
        ):
            return True
        if version.selection == {"kind": "branch", "name": repository.default_branch}:
            latest = await db.scalar(
                select(WorkspaceVersion.commit_sha)
                .where(
                    WorkspaceVersion.repository_id == version.repository_id,
                    WorkspaceVersion.selection == version.selection,
                    WorkspaceVersion.status == "ready",
                )
                .order_by(
                    WorkspaceVersion.created_at.desc(), WorkspaceVersion.id.desc()
                )
                .limit(1)
            )
            return latest == version.commit_sha
        return False

    async def _candidates(self, anchor, result):
        """Physical GC is reserved. Publication shares Job → version → guard."""
        async with self.factory.begin() as db:
            repository = await lock_maintenance(db, anchor)
            now = await db.scalar(select(func.clock_timestamp()))
            cutoff = now - timedelta(days=self.settings.workspace_unused_days)
            version_after, guard_after = self._candidate_after.get(
                anchor.cache_repository_id, (None, None)
            )
            # Permanent evidence is never a lifetime cap. Rotate bounded pages of
            # potentially actionable rows, including protected rows, on each visit.
            version_query = select(WorkspaceVersion).where(
                WorkspaceVersion.cache_repository_id == anchor.cache_repository_id,
                WorkspaceVersion.status.in_(("ready", "unavailable")),
                WorkspaceVersion.pin_ref.is_not(None),
                WorkspaceVersion.last_used_at <= cutoff,
            )
            if version_after is not None:
                version_query = version_query.where(WorkspaceVersion.id > version_after)
            versions = list(
                await db.scalars(version_query.order_by(WorkspaceVersion.id).limit(100))
            )
            guard_query = select(WorkspaceProcessGuard).where(
                WorkspaceProcessGuard.repository_id == anchor.cache_repository_id,
                WorkspaceProcessGuard.kind == "write",
                WorkspaceProcessGuard.state == "drained",
                WorkspaceProcessGuard.proof.in_(("physical", "offline_verified")),
                WorkspaceProcessGuard.drained_at.is_not(None),
                ~exists().where(
                    WorkspaceVersion.cache_repository_id == anchor.cache_repository_id,
                    WorkspaceVersion.pin_ref
                    == "refs/devflow/pins/" + WorkspaceProcessGuard.operation_id,
                ),
            )
            if guard_after is not None:
                guard_query = guard_query.where(
                    WorkspaceProcessGuard.operation_id > guard_after
                )
            guards = list(
                await db.scalars(
                    guard_query.order_by(WorkspaceProcessGuard.operation_id).limit(100)
                )
            )
            job_ids = {v.job_id for v in versions} | {
                UUID(g.lease["job_id"]) for g in guards if g.lease
            }
            jobs = {
                job.id: job
                for job in await db.scalars(
                    select(Job)
                    .where(Job.id.in_(job_ids))
                    .order_by(Job.id)
                    .with_for_update()
                )
            }
            for version in versions:
                await db.refresh(version, with_for_update=True)
            for guard in guards:
                await db.refresh(guard, with_for_update=True)
            removed = []
            for version in versions:
                result.examined += 1
                if (
                    version.status not in {"ready", "unavailable"}
                    or not version.pin_ref
                    or version.last_used_at > cutoff
                    or await self._protected(db, version, repository)
                ):
                    continue
                if await db.scalar(
                    select(
                        exists().where(
                            WorkspaceVersion.cache_repository_id
                            == anchor.cache_repository_id,
                            WorkspaceVersion.id != version.id,
                            WorkspaceVersion.pin_ref == version.pin_ref,
                        )
                    )
                ):
                    continue
                version.status, version.error = "unavailable", "version_unavailable"
                removed.append((version.id, version.pin_ref, version.commit_sha))
            orphans = []
            for guard in guards:
                pin = f"refs/devflow/pins/{guard.operation_id}"
                if (
                    await db.scalar(
                        select(
                            exists().where(
                                WorkspaceVersion.cache_repository_id
                                == anchor.cache_repository_id,
                                WorkspaceVersion.pin_ref == pin,
                            )
                        )
                    )
                    or guard.state != "drained"
                    or guard.proof not in {"physical", "offline_verified"}
                    or guard.drained_at is None
                    or not guard.lease
                ):
                    continue
                job = jobs.get(UUID(guard.lease["job_id"]))
                if job is None or (
                    job.status == "running"
                    and str(job.lease_owner) == guard.lease["owner"]
                    and job.lease_generation == guard.lease["generation"]
                    and job.attempt == guard.lease["attempt"]
                ):
                    continue
                # Drain can precede durable lease revocation by an arbitrary
                # interval. Both facts must be old enough before orphan removal.
                if max(guard.drained_at, job.updated_at) > now - timedelta(
                    seconds=self.settings.workspace_orphan_grace_seconds
                ):
                    continue
                orphans.append(guard.operation_id)
        # Advance only after the metadata transaction commits. An empty page
        # wraps, so inserts below the cursor and interrupted removals are revisited.
        self._candidate_after[anchor.cache_repository_id] = (
            versions[-1].id if versions else None,
            guards[-1].operation_id if guards else None,
        )
        return removed, orphans

    async def sweep(self):
        result = RetentionResult()
        deadline = time.monotonic() + 10
        async with self.factory() as db:
            query = select(WorkspaceVersion.cache_repository_id).distinct()
            if self._after is not None:
                query = query.where(WorkspaceVersion.cache_repository_id > self._after)
            repositories = list(
                await db.scalars(
                    query.order_by(WorkspaceVersion.cache_repository_id).limit(10)
                )
            )
        if not repositories:
            self._after = None
        for numeric in repositories:
            if time.monotonic() >= deadline:
                break
            self._after = numeric
            async with self.factory() as db:
                anchor = await db.scalar(
                    select(WorkspaceVersion)
                    .where(
                        WorkspaceVersion.cache_repository_id == numeric,
                    )
                    .order_by(WorkspaceVersion.id)
                    .limit(1)
                )
            guard = None
            cancel = threading.Event()
            try:
                async with asyncio.timeout(max(0, deadline - time.monotonic())):
                    guard = await reserve_guard(
                        self.factory,
                        self.settings,
                        anchor.id,
                        kind="gc",
                        deadline=deadline,
                        lock_path=self.cache.lock_path(numeric),
                    )
                    removed, orphans = await self._candidates(anchor, result)
                    if not removed and not orphans:
                        continue

                    def clean(
                        guard=guard,
                        cancel=cancel,
                        removed=removed,
                        orphans=orphans,
                        numeric=numeric,
                    ):
                        with self.cache.operation(
                            guard, deadline=deadline, cancel=cancel
                        ):
                            for _, pin, sha in removed:
                                self.cache.remove_attempt(
                                    numeric, pin.rsplit("/", 1)[1], sha
                                )
                            for operation_id in orphans:
                                self.cache.remove_attempt(numeric, operation_id)
                            self.cache.collect(numeric)

                    await owned_thread(clean, cancel)
                    async with self.factory.begin() as db:
                        await lock_maintenance(db, anchor)
                        for identity, pin, _ in removed:
                            row = await db.get(
                                WorkspaceVersion, identity, with_for_update=True
                            )
                            if row.status == "unavailable" and row.pin_ref == pin:
                                row.pin_ref = None
                                result.evicted += 1
                        for operation_id in orphans:
                            row = await db.get(
                                WorkspaceProcessGuard,
                                operation_id,
                                with_for_update=True,
                            )
                            if row is not None and row.state == "drained":
                                await db.delete(row)
                    result.orphans += len(orphans)
            except (AppError, WorkspaceProcessError, TimeoutError):
                result.deferred += 1
            finally:
                cancel.set()
                if guard:
                    await asyncio.shield(revoke_reservation(self.factory, guard))
        return result
