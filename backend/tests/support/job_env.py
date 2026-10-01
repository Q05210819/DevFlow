"""Job test setup shares the real repository authorization boundary."""

import pytest_asyncio

from support.repository_env import repo_env as repo_env  # noqa: PLC0414


@pytest_asyncio.fixture
async def job_env(repo_env):
    from devflow.jobs.service import JobService

    env = repo_env
    env.repository = await env.service.register(env.actor, 11, 101)
    env.jobs = JobService(env.factory, env.policy)
    return env
