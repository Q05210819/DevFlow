"""Real PG/policy/Jobs, with the private chat service on top.

Only upstream GitHub HTTP is replaced by the repository fixture. Nothing here
executes a model; Task 1 proves durable private metadata, not answer quality.
"""

import pytest_asyncio

from support.job_env import job_env as job_env  # noqa: PLC0414
from support.knowledge_env import knowledge_env as knowledge_env  # noqa: PLC0414
from support.repository_env import repo_env as repo_env  # noqa: PLC0414


@pytest_asyncio.fixture
async def chat_env(knowledge_env):
    from devflow.chat.service import ChatService

    env = knowledge_env
    env.chat = ChatService(env.factory, env.policy)

    async def other_user(*repository_ids, github_id=43, role="member"):
        """An authorized account that still must never see another's chat."""
        from pydantic import SecretStr

        from devflow.auth.models import User
        from devflow.auth.types import Actor
        from devflow.github.models import GitHubCredential

        async with env.factory.begin() as db:
            user = User(github_id=github_id, role=role)
            db.add(user)
            await db.flush()
            db.add(
                GitHubCredential(
                    user_id=user.id,
                    access_token_encrypted=env.cipher.encrypt(
                        SecretStr("other-user-token")
                    ),
                )
            )
            actor = Actor(user.id, user.github_id, user.role, user.auth_version)
        for repository_id in repository_ids or (env.repository.id,):
            await env.policy.require(actor, repository_id)
        return actor

    async def revoke(repository_id):
        from sqlalchemy import update

        from devflow.repositories.models import Repository

        async with env.factory.begin() as db:
            await db.execute(
                update(Repository)
                .where(Repository.id == repository_id)
                .values(
                    available=False,
                    auth_version=Repository.auth_version + 1,
                )
            )

    env.other_user, env.revoke = other_user, revoke
    return env
