"""Ordered local authorization fences shared by workspace lifecycle operations."""

from uuid import UUID

from sqlalchemy import select

from devflow.errors import AppError
from devflow.github.models import GitHubInstallation
from devflow.repositories.models import Repository
from devflow.repositories.policy import check_actor
from devflow.sync.scope import authorized


def denied(code="permission_revoked"):
    return AppError(code, 404, "Workspace scope unavailable")


def scope_of(repository, installation):
    return {
        "repository_id": str(repository.id),
        "github_repository_id": repository.github_repository_id,
        "installation_id": installation.installation_id,
        "installation_version": installation.auth_version,
        "repository_version": repository.auth_version,
        "full_name": repository.full_name,
    }


async def capture(policy, actor, repository_id, *, fresh):
    await policy.require(actor, repository_id, fresh=fresh)
    _, key = await policy.local(actor, repository_id)
    async with policy.factory() as db:
        repository = await db.get(Repository, repository_id)
        installation = await db.scalar(
            select(GitHubInstallation).where(
                GitHubInstallation.installation_id == key[5]
            )
        )
        if repository is None or installation is None:
            raise denied()
        result = scope_of(repository, installation)
        if (
            result["installation_version"],
            result["repository_version"],
            result["installation_id"],
        ) != (key[3], key[4], key[5]):
            raise denied()
        return result


async def lock_scopes(db, scopes, settings, *, actor=None):
    if actor is not None:
        await check_actor(db, actor, lock=True)
    installations = {}
    for identity in sorted({item["installation_id"] for item in scopes}):
        installations[identity] = await db.scalar(
            select(GitHubInstallation)
            .where(GitHubInstallation.installation_id == identity)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    for expected in sorted(scopes, key=lambda item: item["repository_id"]):
        repository = await db.scalar(
            select(Repository)
            .where(Repository.id == UUID(expected["repository_id"]))
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        installation = installations.get(expected["installation_id"])
        if (
            repository is None
            or installation is None
            or not authorized(installation, repository, settings)
            or scope_of(repository, installation) != expected
        ):
            raise denied()


async def recheck(policy, actor, scopes):
    for expected in scopes:
        _, key = await policy.local(actor, UUID(expected["repository_id"]))
        if (key[3], key[4], key[5]) != (
            expected["installation_version"],
            expected["repository_version"],
            expected["installation_id"],
        ):
            raise denied()


async def lock_maintenance(db, version):
    """Internal GC owns cached bytes, never remote content or publication."""
    identities = [UUID(scope["repository_id"]) for scope in version.scopes]
    rows = list(
        await db.scalars(select(Repository).where(Repository.id.in_(identities)))
    )
    installation_ids = sorted({row.installation_id for row in rows})
    for identity in installation_ids:
        await db.scalar(
            select(GitHubInstallation)
            .where(GitHubInstallation.installation_id == identity)
            .with_for_update()
        )
    locked = {}
    for scope in sorted(version.scopes, key=lambda item: item["repository_id"]):
        row = await db.scalar(
            select(Repository)
            .where(Repository.id == UUID(scope["repository_id"]))
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if (
            row is None
            or row.installation_id not in installation_ids
            or row.github_repository_id != scope["github_repository_id"]
        ):
            raise denied()
        locked[row.id] = row
    base = locked.get(version.repository_id)
    if base is None or base.github_repository_id != version.cache_repository_id:
        raise denied()
    return base
