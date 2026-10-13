"""Background authorization never impersonates a user or promotes lifecycle state."""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select, text

from devflow.github.models import GitHubInstallation
from devflow.repositories.models import Repository
from devflow.sync.models import RepositorySync


@dataclass(frozen=True, slots=True)
class BackgroundScope:
    repository_id: UUID
    github_repository_id: int
    installation_id: int
    installation_version: int
    repository_version: int
    full_name: str


@asynccontextmanager
async def transaction(factory):
    async with asyncio.timeout(5), factory.begin() as db:
        await db.execute(text("SET LOCAL statement_timeout = '4s'"))
        await db.execute(text("SET LOCAL lock_timeout = '2s'"))
        yield db


async def lock_scope(db, repository_id, *, skip_locked=False):
    """Read the binding, then lock Installation -> Repository -> SyncState.

    The initial binding is only a lock-order hint; recheck after acquiring rows.
    Callers must not hold Job before invoking this function.
    """
    installation_id = await db.scalar(
        select(Repository.installation_id).where(Repository.id == repository_id)
    )
    installation = await db.scalar(
        select(GitHubInstallation)
        .where(GitHubInstallation.installation_id == installation_id)
        .with_for_update(skip_locked=skip_locked)
    )
    if installation is None:
        return None
    repository = await db.scalar(
        select(Repository)
        .where(Repository.id == repository_id)
        .with_for_update(skip_locked=skip_locked)
        .execution_options(populate_existing=True)
    )
    if repository is None or repository.installation_id != installation_id:
        return None
    state = await db.scalar(
        select(RepositorySync)
        .where(RepositorySync.repository_id == repository_id)
        .with_for_update(skip_locked=skip_locked)
    )
    return installation, repository, state


def scope_of(installation, repository):
    return BackgroundScope(
        repository.id,
        repository.github_repository_id,
        installation.installation_id,
        installation.auth_version,
        repository.auth_version,
        repository.full_name,
    )


def authorized(installation, repository, settings, expected=None):
    return (
        repository.enabled
        and repository.available
        and not repository.needs_verification
        and installation.available
        and not installation.needs_verification
        and (
            settings is None
            or installation.installation_id in settings.github_allowed_installation_ids
        )
        and (expected is None or scope_of(installation, repository) == expected)
    )
