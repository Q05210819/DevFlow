"""The only supported handler performs user-authorized metadata reads."""

from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from devflow.auth.models import User
from devflow.auth.service import unauthenticated
from devflow.auth.types import Actor
from devflow.errors import AppError
from devflow.github.client import GitHubClient, not_found
from devflow.github.credentials import CredentialService
from devflow.jobs.models import Job
from devflow.jobs.schemas import ProbeResult
from devflow.repositories.policy import RepositoryPolicy


async def run_probe(
    job_id: UUID,
    factory: async_sessionmaker[AsyncSession],
    policy: RepositoryPolicy,
    github: GitHubClient,
    credentials: CredentialService,
) -> ProbeResult:
    async with factory() as session:
        row = (
            await session.execute(
                select(Job.repository_id, User)
                .join(User, User.id == Job.requested_by)
                .where(Job.id == job_id)
            )
        ).first()
        if row is None:
            raise not_found()
        repository_id, user = row
        if not user.enabled:
            raise unauthenticated()
        actor = Actor(user.id, user.github_id, user.role, user.auth_version)

    _, snapshot = await policy.local(actor, repository_id)
    # Queue.fail records denial together with the fenced state/event transition.
    view = await policy.require(actor, repository_id, fresh=True, audit=False)
    token = await credentials.get_user_token(actor)
    page = 1
    while page is not None:
        try:
            items = await github.list_repositories(token, view.installation_id, page)
        except AppError as error:
            if error.code == "reauth_required":
                await credentials.invalidate_user_token(actor, token)
            raise
        for item in items.items:
            if item.id != view.github_repository_id:
                continue
            _, current = await policy.local(actor, repository_id)
            if current != snapshot:
                raise not_found()
            async with factory() as session:
                checked_at = await session.scalar(select(func.clock_timestamp()))
            return ProbeResult(
                github_repository_id=item.id,
                full_name=item.full_name,
                private=item.private,
                default_branch=item.default_branch,
                checked_at=checked_at,
            )
        page = items.next_page
    raise not_found()
