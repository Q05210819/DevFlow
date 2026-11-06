"""Atomic failure accounting for imports and index builds.

Both counters are cumulative across successor Jobs and survive a Worker
restart, so an abandoned attempt is never silently forgotten.
"""

from pydantic import ValidationError
from sqlalchemy import func, select

from devflow.auth.models import User
from devflow.github.models import GitHubInstallation
from devflow.jobs.models import Job
from devflow.jobs.schemas import KnowledgeIndexInput
from devflow.knowledge.models import (
    ImportRepositoryEpoch,
    IndexVersion,
    KnowledgeCorpus,
    KnowledgeImport,
)
from devflow.knowledge.service import KnowledgeService
from devflow.repositories.models import Repository

UNFINISHED_BUILD = frozenset({"queued", "building"})
MAX_BUILD_FAILURES = 3


async def fail_import(queue, identity, code, retryable, retry_at=None, *, lease=None):
    async with queue._transaction() as db:
        raw = await db.get(Job, identity)
        if raw is None or raw.kind != "knowledge.import":
            return False
        imported = await db.scalar(
            select(KnowledgeImport).where(KnowledgeImport.id == raw.input["import_id"])
        )
        if imported is None:
            return False
        # No remote proof is required to settle failure. Retain local lock order
        # even when a user/source is now revoked; never publish through this path.
        await KnowledgeService(queue.factory, None)._capacity(
            db, imported.repository_id
        )
        await db.execute(
            select(User.id).where(User.id == imported.actor_id).with_for_update()
        )
        epochs = list(
            await db.scalars(
                select(ImportRepositoryEpoch).where(
                    ImportRepositoryEpoch.import_id == imported.id
                )
            )
        )
        for installation in sorted({epoch.installation_id for epoch in epochs}):
            await db.execute(
                select(GitHubInstallation.installation_id)
                .where(GitHubInstallation.installation_id == installation)
                .with_for_update()
            )
        for repository in sorted({epoch.repository_id for epoch in epochs}, key=str):
            await db.execute(
                select(Repository.id)
                .where(Repository.id == repository)
                .with_for_update()
            )
        if lease is not None:
            job, now = await queue._owned(db, lease)
        else:
            job = await db.scalar(
                select(Job)
                .where(Job.id == identity, Job.status == "running")
                .with_for_update(skip_locked=True)
                .execution_options(populate_existing=True)
            )
            now = await db.scalar(select(func.clock_timestamp()))
            if job is not None and job.lease_expires_at > now:
                job = None
        if job is None:
            return False
        corpus = await db.get(
            KnowledgeCorpus, imported.repository_id, with_for_update=True
        )
        imported = await db.get(
            KnowledgeImport, imported.id, with_for_update=True, populate_existing=True
        )
        if imported.status not in {"queued", "building"}:
            retryable = False
        else:
            imported.failures = min(3, imported.failures + 1)
            retryable = (
                retryable
                and imported.failures < 3
                and now < imported.deadline
                and corpus.target_generation == imported.target_generation
            )
            if corpus.target_generation != imported.target_generation:
                imported.status = "superseded"
            elif not retryable or job.attempt >= job.max_attempts:
                imported.status = "failed"
        if lease is None:
            job.lease_generation += 1
        await queue._failure(db, job, now, code, retryable, retry_at)
        return True


async def fail_index_build(
    queue, identity, code, retryable, retry_at=None, *, lease=None
):
    """Account one failed batch attempt against the candidate build.

    The candidate owns a fixed 24-hour window and at most three failures in
    total. Successor Jobs use fresh per-Job attempt budgets, so only this row
    counter can stop a build that keeps failing in later batches.
    """

    async with queue._transaction() as db:
        raw = await db.get(Job, identity)
        if raw is None or raw.kind != "knowledge.index_batch":
            return False
        try:
            index_id = KnowledgeIndexInput.model_validate(raw.input).index_id
        except (ValidationError, TypeError, ValueError):
            return False
        if lease is not None:
            job, now = await queue._owned(db, lease)
        else:
            job = await db.scalar(
                select(Job)
                .where(Job.id == identity, Job.status == "running")
                .with_for_update(skip_locked=True)
                .execution_options(populate_existing=True)
            )
            now = await db.scalar(select(func.clock_timestamp()))
            if job is not None and job.lease_expires_at > now:
                job = None
        if job is None:
            return False
        row = await db.get(
            IndexVersion,
            index_id,
            with_for_update=True,
            populate_existing=True,
        )
        if row is None or row.status not in UNFINISHED_BUILD:
            retryable = False
        else:
            row.failures = min(MAX_BUILD_FAILURES, row.failures + 1)
            retryable = (
                retryable and row.failures < MAX_BUILD_FAILURES and now < row.deadline
            )
            if not retryable or job.attempt >= job.max_attempts:
                row.status = "failed"
                row.queryable = False
            row.updated_at = now
        if lease is None:
            job.lease_generation += 1
        await queue._failure(db, job, now, code, retryable, retry_at)
        return True
