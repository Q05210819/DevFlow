"""User-scoped authorization. Remote observations never override newer local epochs."""

import time
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from devflow.audit.service import AuditEntry, write_audit
from devflow.auth.models import User
from devflow.auth.service import unauthenticated
from devflow.auth.types import Actor
from devflow.config import Settings
from devflow.errors import AppError
from devflow.github.client import GitHubClient, not_found
from devflow.github.credentials import CredentialService
from devflow.github.models import GitHubInstallation
from devflow.github.schemas import GitHubInstallationInfo, GitHubRepository
from devflow.repositories.models import Repository
from devflow.repositories.schemas import RepositoryView


@dataclass(frozen=True)
class Verification:
    installation_version: int
    repository_version: int | None
    previous_installation_id: int | None
    installation: GitHubInstallationInfo
    repository: GitHubRepository


async def check_actor(session: AsyncSession, actor: Actor, *, lock=False) -> User:
    query = select(User).where(User.id == actor.id)
    if lock:
        query = query.with_for_update()
    user = await session.scalar(query)
    if (
        not user
        or not user.enabled
        or user.auth_version != actor.auth_version
        or user.github_id != actor.github_id
    ):
        raise unauthenticated()
    return user


class RepositoryPolicy:
    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        github: GitHubClient,
        credentials: CredentialService,
        settings: Settings,
    ):
        self.factory, self.github, self.credentials, self.settings = (
            factory,
            github,
            credentials,
            settings,
        )
        self._cache: dict[tuple, float] = {}

    def allowed(self, installation_id: int) -> bool:
        return installation_id in self.settings.github_allowed_installation_ids

    async def user_request(self, actor, token, method, *args):
        try:
            return await method(token, *args)
        except AppError as error:
            if error.code == "reauth_required":
                await self.credentials.invalidate_user_token(actor, token)
            raise

    async def installation_snapshot(self, actor: Actor, installation_id: int) -> int:
        if not self.allowed(installation_id):
            raise not_found()
        async with self.factory.begin() as session:
            await check_actor(session, actor, lock=True)
            await session.execute(
                insert(GitHubInstallation)
                .values(installation_id=installation_id)
                .on_conflict_do_nothing(index_elements=["installation_id"])
            )
            installation = await session.scalar(
                select(GitHubInstallation).where(
                    GitHubInstallation.installation_id == installation_id
                )
            )
            if not installation.available and not installation.needs_verification:
                raise not_found()
            return installation.auth_version

    async def verify(
        self, actor: Actor, installation_id: int, github_repository_id: int
    ) -> Verification:
        epoch = await self.installation_snapshot(actor, installation_id)
        async with self.factory() as session:
            repository = await session.scalar(
                select(Repository).where(
                    Repository.github_repository_id == github_repository_id
                )
            )
            if repository and (
                not repository.enabled
                or (not repository.available and not repository.needs_verification)
            ):
                raise not_found()
            repo_epoch = repository.auth_version if repository else None
            previous_installation = repository.installation_id if repository else None
        token = await self.credentials.get_user_token(actor)
        page = 1
        while page is not None:
            result = await self.user_request(
                actor, token, self.github.list_installations, page
            )
            if any(
                item.id == installation_id
                and item.app_id == self.settings.github_app_id
                and item.suspended_at is None
                for item in result.items
            ):
                break
            page = result.next_page
        else:
            raise not_found()
        installation = await self.github.get_installation(installation_id)
        page = 1
        while page is not None:
            result = await self.user_request(
                actor, token, self.github.list_repositories, installation_id, page
            )
            for item in result.items:
                if item.id == github_repository_id:
                    return Verification(
                        epoch, repo_epoch, previous_installation, installation, item
                    )
            page = result.next_page
        raise not_found()

    async def lock_verified(
        self, session: AsyncSession, actor: Actor, proof: Verification
    ):
        """Caller owns transaction; lock order User → Installation → Repository."""
        user = await check_actor(session, actor, lock=True)
        installation = await session.scalar(
            select(GitHubInstallation)
            .where(GitHubInstallation.installation_id == proof.installation.id)
            .with_for_update()
        )
        if (
            not self.allowed(proof.installation.id)
            or not installation
            or installation.auth_version != proof.installation_version
        ):
            raise not_found()
        # Serialize global ID insertion even when no Repository row exists yet.
        await session.execute(
            select(
                func.pg_advisory_xact_lock(
                    func.hashtextextended(
                        "devflow:repository:" + str(proof.repository.id), 0
                    )
                )
            )
        )
        repository = await session.scalar(
            select(Repository)
            .where(Repository.github_repository_id == proof.repository.id)
            .with_for_update()
        )
        if proof.repository_version is not None:
            if (
                not repository
                or repository.auth_version != proof.repository_version
                or repository.installation_id != proof.previous_installation_id
            ):
                raise not_found()
        elif repository and (
            repository.auth_version != 1
            or repository.installation_id != proof.installation.id
        ):
            raise not_found()
        if repository and (
            not repository.enabled
            or (not repository.available and not repository.needs_verification)
        ):
            raise not_found()
        return user, installation, repository

    @staticmethod
    def promote(
        installation: GitHubInstallation, repository: Repository, proof: Verification
    ):
        installation.app_id = proof.installation.app_id
        installation.account_id = proof.installation.account_id
        installation.available = True
        installation.needs_verification = False
        installation.verified_at = func.clock_timestamp()
        if repository.id is not None and (
            repository.installation_id != proof.installation.id
            or repository.full_name != proof.repository.full_name
            or repository.private != proof.repository.private
            or repository.default_branch != proof.repository.default_branch
        ):
            repository.auth_version += 1
        repository.installation_id = proof.installation.id
        repository.full_name = proof.repository.full_name
        repository.private = proof.repository.private
        repository.default_branch = proof.repository.default_branch
        repository.available = True
        repository.needs_verification = False
        repository.verified_at = func.clock_timestamp()

    async def local(self, actor: Actor, repository_id: UUID):
        async with self.factory() as session:
            # One statement reads all authorization versions from one PG snapshot.
            result = (
                await session.execute(
                    select(User, Repository, GitHubInstallation)
                    .select_from(User)
                    .join(Repository, Repository.id == repository_id)
                    .join(
                        GitHubInstallation,
                        GitHubInstallation.installation_id
                        == Repository.installation_id,
                    )
                    .where(User.id == actor.id)
                )
            ).first()
            if not result:
                await check_actor(session, actor)
                raise not_found()
            user, repository, installation = result
            if (
                not user.enabled
                or user.auth_version != actor.auth_version
                or user.github_id != actor.github_id
            ):
                raise unauthenticated()
            if (
                not repository.enabled
                or not repository.available
                or repository.needs_verification
                or not installation.available
                or installation.needs_verification
                or not self.allowed(installation.installation_id)
            ):
                raise not_found()
            key = (
                actor.id,
                repository.id,
                user.auth_version,
                installation.auth_version,
                repository.auth_version,
                installation.installation_id,
            )
            return RepositoryView.model_validate(repository), key

    async def require(
        self, actor: Actor, repository_id: UUID, *, fresh: bool = False
    ) -> RepositoryView:
        try:
            return await self._require(actor, repository_id, fresh=fresh)
        except AppError as error:
            self._cache = {
                key: deadline
                for key, deadline in self._cache.items()
                if key[:2] != (actor.id, repository_id)
            }
            await self.audit_denial(actor, error, repository_id)
            raise

    async def audit_denial(
        self, actor: Actor, error: AppError, repository_id: UUID | None = None
    ):
        if error.status not in (403, 404):
            return
        async with self.factory.begin() as session:
            await write_audit(
                session,
                AuditEntry(
                    actor_id=actor.id,
                    action="permission.denied",
                    resource_type="repository",
                    resource_id=repository_id,
                    outcome="failure",
                    reason_code="resource_not_found"
                    if error.status == 404
                    else "forbidden",
                ),
            )

    async def _require(
        self, actor: Actor, repository_id: UUID, *, fresh: bool
    ) -> RepositoryView:
        view, key = await self.local(actor, repository_id)
        started = time.monotonic()
        if not fresh and self._cache.get(key, 0) > started:
            return view
        # Failed fresh checks cannot leave an earlier success usable.
        self._cache.pop(key, None)
        proof = await self.verify(
            actor, view.installation_id, view.github_repository_id
        )
        async with self.factory.begin() as session:
            user, installation, repository = await self.lock_verified(
                session, actor, proof
            )
            if (
                repository is None
                or repository.id != repository_id
                or not repository.available
                or repository.needs_verification
                or not installation.available
                or installation.needs_verification
            ):
                raise not_found()
            verified_key = (
                actor.id,
                repository.id,
                user.auth_version,
                installation.auth_version,
                repository.auth_version,
                installation.installation_id,
            )
            if verified_key != key:
                raise not_found()
            # A read proves access to this exact snapshot. Only registration may
            # promote lifecycle state or persist a changed repository binding.
            verified_view = RepositoryView(
                id=repository.id,
                github_repository_id=proof.repository.id,
                installation_id=proof.installation.id,
                full_name=proof.repository.full_name,
                private=proof.repository.private,
                default_branch=proof.repository.default_branch,
            )
        _, current_key = await self.local(actor, repository_id)
        if current_key != verified_key:
            raise not_found()
        self._cache = {
            k: deadline for k, deadline in self._cache.items() if deadline > started
        }
        if len(self._cache) >= 1024:
            self._cache.pop(next(iter(self._cache)))
        self._cache[verified_key] = started + 60
        return verified_view
