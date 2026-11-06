"""Real PG/policy; only upstream HTTP and already-published Workspace are fixtures."""

import hashlib
from datetime import timedelta
from uuid import uuid4

import pytest_asyncio
from sqlalchemy import func, select

from support.job_env import job_env as job_env  # noqa: PLC0414
from support.job_env import repo_env as repo_env  # noqa: PLC0414
from support.repository_env import installation, repository


@pytest_asyncio.fixture
async def knowledge_env(job_env):
    env = job_env

    async def fork():
        env.installations = {1: ([installation(), installation(12)], None)}
        env.repositories[(12, 1)] = ([repository(202, "other/fork")], None)
        return await env.service.register(env.actor, 12, 202)

    async def workspace(source=None, *, sha="b" * 40):
        from devflow.jobs.models import Job
        from devflow.workspace.models import WorkspaceVersion
        from devflow.workspace.scope import capture

        source = source or env.repository
        scopes = [await capture(env.policy, env.actor, env.repository.id, fresh=True)]
        if source.id != env.repository.id:
            scopes.append(await capture(env.policy, env.actor, source.id, fresh=True))
        async with env.factory.begin() as db:
            job = Job(
                kind="repository.workspace",
                repository_id=env.repository.id,
                requested_by=None,
                input={},
                input_hash="a" * 64,
                idempotency_key=uuid4().hex,
                status="succeeded",
            )
            db.add(job)
            await db.flush()
            version = WorkspaceVersion(
                repository_id=env.repository.id,
                source_repository_id=source.id,
                cache_repository_id=101,
                requested_by=env.actor.id,
                idempotency_key=uuid4().hex,
                input_hash="a" * 64,
                selection={"kind": "branch", "name": "main"},
                source={
                    "selection": {"kind": "branch", "name": "main"},
                    "ref": "refs/heads/main",
                    "source_repository_id": source.github_repository_id,
                    "transfer_repository_id": 101,
                },
                scopes=scopes,
                commit_sha=sha,
                tree_oid="c" * 40,
                status="ready",
                pin_ref="refs/devflow/pins/" + uuid4().hex,
                job_id=job.id,
            )
            db.add(version)
            await db.flush()
            return version

    async def due(job_id):
        from devflow.jobs.models import Job

        async with env.factory.begin() as db:
            job = await db.get(Job, job_id)
            # Fixture data only: the host clock is known to jump, never patch production.
            job.available_at = await db.scalar(
                select(func.clock_timestamp())
            ) - timedelta(days=1)

    env.fork, env.workspace, env.due = fork, workspace, due
    env.digest = lambda body: hashlib.sha256(body).hexdigest()
    return env
