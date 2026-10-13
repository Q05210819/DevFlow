"""Real registered repositories and PostgreSQL; only GitHub HTTP is synthetic."""

from datetime import UTC, datetime, timedelta

import httpx
import pytest_asyncio

from support.repository_env import repo_env as repo_env  # noqa: PLC0414


@pytest_asyncio.fixture
async def sync_env(repo_env):
    from devflow.github.client import GitHubClient
    from devflow.github.data import GitHubDataClient
    from devflow.github.installations import InstallationTokens
    from devflow.jobs.queue import Queue
    from devflow.sync.scheduler import SyncScheduler
    from devflow.sync.service import SyncService

    env = repo_env
    env.repository = await env.service.register(env.actor, 11, 101)
    env.data_calls = []

    async def remote(request):
        env.data_calls.append(request)
        if request.method == "POST":
            return httpx.Response(
                201,
                json={
                    "token": "installation-test-token",
                    "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
                    "permissions": {"metadata": "read", "issues": "read"},
                    "repositories": [{"id": 101}],
                },
            )
        return httpx.Response(200, json=[])

    async with httpx.AsyncClient(transport=httpx.MockTransport(remote)) as http:
        client = GitHubClient(http, env.settings)
        env.data = GitHubDataClient(client)
        env.tokens = InstallationTokens(client)
        env.queue = Queue(env.factory, settings=env.settings)
        env.sync = SyncService(env.factory, env.settings)
        env.scheduler = SyncScheduler(env.factory, env.settings)
        yield env
