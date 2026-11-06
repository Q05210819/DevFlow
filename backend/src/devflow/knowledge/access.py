"""Fresh user grants and a single ordered local fence for all source dependencies."""

from sqlalchemy import select

from devflow.github.client import not_found
from devflow.github.models import GitHubInstallation
from devflow.knowledge.schemas import RepositoryEpoch, SourceGrant
from devflow.repositories.models import Repository
from devflow.repositories.policy import check_actor
from devflow.sync.scope import authorized


class KnowledgeAccess:
    def __init__(self, policy):
        self.policy = policy

    async def verify(self, actor, repository_ids, *, fresh):
        epochs = []
        if not repository_ids:
            raise not_found()
        for repository_id in sorted(set(repository_ids), key=str):
            _, before = await self.policy.local(actor, repository_id)
            await self.policy.require(actor, repository_id, fresh=fresh)
            _, after = await self.policy.local(actor, repository_id)
            if before != after:
                raise not_found()
            # RepositoryPolicy's tuple is deliberately confined to this adapter.
            actor_id, repo, user, installation, repository, binding = after
            if actor_id != actor.id:
                raise not_found()
            epochs.append(
                RepositoryEpoch(repo, binding, user, installation, repository)
            )
        return SourceGrant(actor, tuple(epochs))

    async def lock(self, session, grant):
        user = await check_actor(session, grant.actor, lock=True)
        await session.refresh(user)
        if not user.enabled or (user.auth_version, user.github_id, user.role) != (
            grant.actor.auth_version,
            grant.actor.github_id,
            grant.actor.role,
        ):
            raise not_found()
        installations = {}
        for identity in sorted(
            {epoch.installation_id for epoch in grant.repository_epochs}
        ):
            installations[identity] = await session.scalar(
                select(GitHubInstallation)
                .where(GitHubInstallation.installation_id == identity)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        for epoch in sorted(
            grant.repository_epochs, key=lambda item: str(item.repository_id)
        ):
            repository = await session.scalar(
                select(Repository)
                .where(Repository.id == epoch.repository_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            installation = installations[epoch.installation_id]
            if (
                repository is None
                or installation is None
                or not authorized(installation, repository, self.policy.settings)
                or user.auth_version != epoch.user_version
                or repository.installation_id != epoch.installation_id
                or repository.auth_version != epoch.repository_version
                or installation.auth_version != epoch.installation_version
            ):
                raise not_found()
