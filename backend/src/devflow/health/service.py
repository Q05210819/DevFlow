"""Independent health demand and lease-fenced deterministic snapshots."""

import hashlib
import json
from dataclasses import dataclass
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

from sqlalchemy import DateTime, and_, case, cast, func, or_, select

from devflow.audit.service import AuditEntry, write_audit
from devflow.errors import AppError
from devflow.facts.models import Fact
from devflow.facts.service import coverage, recheck, source_url
from devflow.health.models import HealthSnapshot
from devflow.health.schemas import (
    ActivityWindow,
    CIRunVersion,
    HealthCounts,
    HealthManifest,
    HealthSnapshotView,
    HealthView,
    InputPopulation,
    PRVersion,
    Selection,
)
from devflow.jobs.models import Job
from devflow.jobs.queue import Queue
from devflow.jobs.schemas import HealthResult
from devflow.jobs.service import JobService, append_event
from devflow.repositories.service import command_receipt
from devflow.sync.models import RepositoryCommandReceipt, RepositorySync
from devflow.sync.scope import (
    BackgroundScope,
    authorized,
    lock_scope,
    scope_of,
    transaction,
)


@dataclass(frozen=True)
class FrozenHealth:
    lease: object
    scope: BackgroundScope
    captured_sequence: int
    snapshot: HealthSnapshotView


def selection(order, total, returned):
    return Selection(
        selection_order=order,
        total_candidates=total,
        returned_count=returned,
        truncated=total > returned,
    )


async def compute_snapshot(db, repository, state, now):
    """All fact publishers hold this same sync-row fence. Python materializes only caps."""
    cov = [
        await coverage(db, repository.id, kind) for kind in ("issue", "pull", "ci_run")
    ]
    eligible = [Fact.repository_id == repository.id, Fact.unavailable.is_(False)]
    attempt_rank = (
        select(
            Fact.id,
            func.row_number()
            .over(
                partition_by=Fact.source_id,
                order_by=Fact.payload["attempt"].as_integer().desc(),
            )
            .label("rank"),
        )
        .where(Fact.repository_id == repository.id, Fact.kind == "ci_run")
        .subquery()
    )
    latest = select(attempt_rank.c.id).where(attempt_rank.c.rank == 1)
    populations, counts, activity = [], {}, []
    for kind, evidence in zip(("issue", "pull", "ci_run"), cov, strict=True):
        conditions = [*eligible, Fact.kind == kind]
        if kind == "ci_run":
            conditions.append(Fact.id.in_(latest))
        total, first, last = (
            await db.execute(
                select(
                    func.count(), func.min(Fact.observed_at), func.max(Fact.observed_at)
                ).where(*conditions)
            )
        ).one()
        populations.append(
            InputPopulation(
                kind=kind, count=total, observed_from=first, observed_until=last
            )
        )
        known = total > 0 or evidence.status in {"complete", "partial"}
        if kind != "ci_run":
            opened = Fact.payload["state"].astext == "open"
            open_count, unassigned, draft, stale = (
                await db.execute(
                    select(
                        func.count().filter(opened),
                        func.count().filter(
                            and_(
                                opened,
                                func.jsonb_array_length(Fact.payload["assignees"]) == 0,
                            )
                        ),
                        func.count().filter(
                            and_(opened, Fact.payload["draft"].as_boolean().is_(True))
                        ),
                        func.count().filter(
                            and_(
                                opened, Fact.source_updated_at < now - timedelta(days=7)
                            )
                        ),
                    ).where(*conditions)
                )
            ).one()
            suffix = "issues" if kind == "issue" else "pulls"
            counts["open_" + suffix] = open_count if known else None
            counts["stale_" + suffix] = stale if known else None
            counts["unassigned_issues" if kind == "issue" else "draft_pulls"] = (
                (unassigned if kind == "issue" else draft) if known else None
            )
        for days in (7, 30):
            start = now - timedelta(days=days)
            actual_start = max(start, evidence.start) if evidence.start else None
            actual_end = min(now, evidence.end) if evidence.end else None
            if actual_start is None or actual_end is None or actual_start > actual_end:
                actual_start = actual_end = created = updated = None
            else:
                created_at = cast(
                    Fact.payload["created_at"].astext, DateTime(timezone=True)
                )
                created, updated = (
                    await db.execute(
                        select(
                            func.count().filter(
                                created_at.between(actual_start, actual_end)
                            ),
                            func.count().filter(
                                Fact.source_updated_at.between(actual_start, actual_end)
                            ),
                        ).where(*conditions)
                    )
                ).one()
            activity.append(
                ActivityWindow(
                    stream=evidence.stream,
                    days=days,
                    requested_start=start,
                    requested_end=now,
                    actual_start=actual_start,
                    actual_end=actual_end,
                    complete=evidence.status == "complete"
                    and actual_start == start
                    and actual_end == now,
                    created_records=created,
                    updated_records=updated,
                )
            )
    pr_conditions = [
        *eligible,
        Fact.kind == "pull",
        Fact.payload["state"].astext == "open",
    ]
    pr_total = await db.scalar(
        select(func.count()).select_from(Fact).where(*pr_conditions)
    )
    pr_rows = (
        await db.execute(
            select(
                Fact.source_id,
                Fact.number,
                Fact.source_url,
                Fact.observed_at,
                Fact.payload["base_sha"].astext.label("base_sha"),
                Fact.payload["head_sha"].astext.label("head_sha"),
            )
            .where(*pr_conditions)
            .order_by(Fact.number, Fact.source_id)
            .limit(100)
        )
    ).all()
    pulls = [
        PRVersion(
            source_id=r.source_id,
            number=r.number,
            base_sha=r.base_sha,
            head_sha=r.head_sha,
            observed_at=r.observed_at,
            source_url=source_url(
                SimpleNamespace(
                    kind="pull",
                    source_id=r.source_id,
                    number=r.number,
                    source_url=r.source_url,
                ),
                repository.full_name,
            ),
        )
        for r in pr_rows
    ]
    default_rank = (
        select(
            Fact.id,
            func.row_number()
            .over(
                partition_by=Fact.payload["workflow_id"].as_integer(),
                order_by=(
                    cast(
                        Fact.payload["created_at"].astext, DateTime(timezone=True)
                    ).desc(),
                    Fact.source_id.desc(),
                ),
            )
            .label("rank"),
        )
        .where(
            Fact.repository_id == repository.id,
            Fact.id.in_(latest),
            Fact.payload["branch"].astext == repository.default_branch,
            repository.default_branch is not None,
        )
        .subquery()
    )
    default_ids = select(default_rank.c.id).where(default_rank.c.rank == 1)
    candidate = [
        *eligible,
        Fact.id.in_(latest),
        or_(
            Fact.id.in_(default_ids),
            Fact.payload["head_sha"].astext.in_([p.head_sha for p in pulls]),
        ),
    ]
    ci_total = await db.scalar(select(func.count()).select_from(Fact).where(*candidate))
    ci_rows = (
        await db.execute(
            select(
                Fact.source_id,
                Fact.source_url,
                Fact.source_updated_at,
                Fact.observed_at,
                Fact.payload["attempt"].as_integer().label("attempt"),
                Fact.payload["workflow_id"].as_integer().label("workflow_id"),
                Fact.payload["head_sha"].astext.label("head_sha"),
                Fact.payload["branch"].astext.label("branch"),
                Fact.payload["status"].astext.label("status"),
                Fact.payload["conclusion"].astext.label("conclusion"),
                Fact.id.in_(default_ids).label("default_branch_latest"),
            )
            .where(*candidate)
            .order_by(
                Fact.source_updated_at.desc().nulls_last(),
                Fact.source_id.desc(),
                Fact.payload["attempt"].as_integer().desc(),
            )
            .limit(200)
        )
    ).all()
    actions = [
        CIRunVersion(
            **{
                k: getattr(r, k)
                for k in (
                    "source_id",
                    "source_updated_at",
                    "observed_at",
                    "attempt",
                    "workflow_id",
                    "head_sha",
                    "branch",
                    "status",
                    "conclusion",
                    "default_branch_latest",
                )
            },
            source_url=source_url(
                SimpleNamespace(
                    kind="ci_run",
                    source_id=r.source_id,
                    number=None,
                    source_url=r.source_url,
                    payload={"attempt": r.attempt},
                ),
                repository.full_name,
            ),
            pr_numbers=[p.number for p in pulls if p.head_sha == r.head_sha],
        )
        for r in ci_rows
    ]
    category = case(
        (Fact.payload["status"].astext != "completed", Fact.payload["status"].astext),
        else_=func.coalesce(Fact.payload["conclusion"].astext, "unknown"),
    )
    categories = dict(
        (
            await db.execute(
                select(category, func.count())
                .where(*eligible, Fact.id.in_(latest))
                .group_by(category)
            )
        ).all()
    )
    observed_actions = populations[2].count > 0 or cov[2].status in {
        "complete",
        "partial",
    }
    manifest = HealthManifest(
        frozen_at=now,
        sync_generation=state.generation,
        requested_sequence=state.requested_sequence,
        processed_sequence=state.processed_sequence,
        populations=populations,
        coverage=cov,
        pulls=pulls,
        pulls_selection=selection("number ASC, source_id ASC", pr_total, len(pulls)),
        actions=actions,
        actions_selection=selection(
            "source_updated_at DESC NULLS LAST, source_id DESC, attempt DESC; latest attempt per run; default workflow latest by created_at/source_id",
            ci_total,
            len(actions),
        ),
    )
    return HealthSnapshotView(
        id=uuid4(),
        generation=state.health_generation,
        generated_at=now,
        thresholds={
            "stale_days": 7,
            "activity_short_days": 7,
            "activity_long_days": 30,
        },
        counts=HealthCounts(
            **counts,
            activity=activity,
            actions_categories=categories if observed_actions else None,
        ),
        coverage=cov,
        manifest=manifest,
    )


async def enqueue_health(db, state, installation, repository):
    if (
        state.active_health_job_id
        or state.health_requested_sequence <= state.health_processed_sequence
    ):
        return
    value = {
        "captured_sequence": state.health_requested_sequence,
        "installation_id": installation.installation_id,
        "installation_version": installation.auth_version,
        "repository_version": repository.auth_version,
    }
    job = Job(
        kind="repository.health",
        repository_id=repository.id,
        requested_by=None,
        input=value,
        input_hash=hashlib.sha256(
            json.dumps(value, sort_keys=True).encode()
        ).hexdigest(),
        idempotency_key=f"health:{state.health_requested_sequence}",
    )
    db.add(job)
    await db.flush()
    state.active_health_job_id = job.id
    state.health_status = "queued"
    state.health_error_code = None
    await append_event(db, job, "queued")
    await write_audit(
        db,
        AuditEntry(
            action="job.submitted",
            resource_type="job",
            resource_id=job.id,
            job_id=job.id,
            outcome="success",
        ),
    )


async def request_health(db, repository_id, *, actor_id=None):
    """Caller owns transaction. Order is Installation -> Repository -> SyncState -> Job."""
    rows = await lock_scope(db, repository_id)
    if rows is None or not authorized(rows[0], rows[1], None):
        raise AppError("resource_not_found", 404, "Resource not found")
    installation, repository, state = rows
    if state is None:
        state = RepositorySync(repository_id=repository_id)
        db.add(state)
        await db.flush()
    if state.active_health_job_id:
        active = await db.scalar(
            select(Job).where(Job.id == state.active_health_job_id).with_for_update()
        )
        if active and (
            active.input.get("installation_id") != installation.installation_id
            or active.input.get("installation_version") != installation.auth_version
            or active.input.get("repository_version") != repository.auth_version
        ):
            now = await db.scalar(select(func.clock_timestamp()))
            await Queue(None)._failure(
                db, active, now, "permission_revoked", False, None
            )
            state.active_health_job_id = None
    state.health_requested_sequence += 1
    await enqueue_health(db, state, installation, repository)
    await write_audit(
        db,
        AuditEntry(
            actor_id=actor_id,
            action="health.requested",
            resource_type="repository",
            resource_id=repository_id,
            outcome="success",
        ),
    )
    return state


async def health_view(db, repo):
    state = await db.get(RepositorySync, repo)
    row = (
        await db.get(HealthSnapshot, state.current_health_id)
        if state and state.current_health_id
        else None
    )
    active = (
        await db.get(Job, state.active_health_job_id)
        if state and state.active_health_job_id
        else None
    )
    return HealthView(
        snapshot=HealthSnapshotView.model_validate(row.result) if row else None,
        refresh_status=state.health_status if state else "idle",
        error_code=state.health_error_code if state else None,
        retry_at=active.available_at
        if active and active.status == "retry_wait"
        else None,
    )


class HealthService:
    def __init__(self, factory, policy, settings):
        self.factory, self.policy, self.settings = factory, policy, settings

    async def _locked(self, db, lease, frozen=None):
        repo = await db.scalar(select(Job.repository_id).where(Job.id == lease.job_id))
        rows = await lock_scope(db, repo) if repo else None
        if rows is None:
            return None
        installation, repository, state = rows
        job, now = await Queue(self.factory)._owned(db, lease)
        if (
            job is None
            or job.kind != "repository.health"
            or state is None
            or not authorized(
                installation,
                repository,
                self.settings,
                frozen.scope if frozen else None,
            )
        ):
            return None
        if any(
            job.input.get(k) != v
            for k, v in {
                "installation_id": installation.installation_id,
                "installation_version": installation.auth_version,
                "repository_version": repository.auth_version,
            }.items()
        ):
            return None
        if frozen and (
            frozen.lease != lease
            or frozen.captured_sequence != job.input.get("captured_sequence")
        ):
            return None
        return installation, repository, state, job, now

    async def freeze(self, lease):
        async with transaction(self.factory) as db:
            rows = await self._locked(db, lease)
            if rows is None:
                raise AppError("permission_revoked", 404, "Health scope unavailable")
            installation, repository, state, job, now = rows
            state.health_generation += 1
            snapshot = await compute_snapshot(db, repository, state, now)
            return FrozenHealth(
                lease,
                scope_of(installation, repository),
                job.input["captured_sequence"],
                snapshot,
            )

    async def publish(self, lease, frozen):
        async with transaction(self.factory) as db:
            rows = await self._locked(db, lease, frozen)
            if rows is None:
                return False
            installation, repository, state, job, now = rows
            snapshot = frozen.snapshot
            db.add(
                HealthSnapshot(
                    id=snapshot.id,
                    repository_id=repository.id,
                    generation=snapshot.generation,
                    rule_version=snapshot.rule_version,
                    generated_at=snapshot.generated_at,
                    manifest=snapshot.manifest.model_dump(mode="json"),
                    result=snapshot.model_dump(mode="json"),
                )
            )
            await db.flush()
            if snapshot.generation > state.published_health_generation:
                state.current_health_id = snapshot.id
                state.published_health_generation = snapshot.generation
            state.health_processed_sequence = max(
                state.health_processed_sequence, frozen.captured_sequence
            )
            job.status, job.error_code = "succeeded", None
            job.result = HealthResult(
                snapshot_id=snapshot.id, generation=snapshot.generation
            ).model_dump(mode="json")
            job.lease_owner = job.lease_expires_at = None
            job.updated_at = now
            await Queue._transition(db, job, "job.succeeded")
            if state.active_health_job_id == job.id:
                state.active_health_job_id = None
                state.health_status, state.health_error_code = "completed", None
                await enqueue_health(db, state, installation, repository)
            return True

    async def latest(self, actor, repo):
        key = await JobService(self.factory, self.policy)._authorize(
            actor, repo, fresh=False
        )
        async with transaction(self.factory) as db:
            result = await health_view(db, repo)
        await recheck(self.policy, actor, repo, key)
        return result

    async def request(self, actor, repo, key):
        jobs = JobService(self.factory, self.policy)
        snapshot = await jobs._authorize(actor, repo, fresh=True)
        try:
            async with transaction(self.factory) as db:
                await jobs._lock_authorization(db, actor, snapshot)
                receipt, digest = await command_receipt(
                    db,
                    actor_id=actor.id,
                    repository_id=repo,
                    operation="health.refresh",
                    idempotency_key=key,
                    normalized_payload={},
                )
                if receipt is None:
                    state = await request_health(db, repo, actor_id=actor.id)
                    db.add(
                        RepositoryCommandReceipt(
                            actor_id=actor.id,
                            repository_id=repo,
                            operation="health.refresh",
                            idempotency_key=key,
                            payload_hash=digest,
                            demand_sequence=state.health_requested_sequence,
                        )
                    )
                    await db.flush()
                result = await health_view(db, repo)
        except AppError as error:
            await self.policy.audit_denial(actor, error, repo)
            raise
        await recheck(self.policy, actor, repo, snapshot)
        return result
