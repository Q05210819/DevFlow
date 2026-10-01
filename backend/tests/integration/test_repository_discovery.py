from uuid import uuid4

import pytest
from support.repository_env import installation, repository
from support.repository_env import repo_env as repo_env  # noqa: PLC0414

from devflow.auth.types import Actor
from devflow.errors import AppError


@pytest.mark.asyncio
async def test_discovery_follows_both_pagination_levels(repo_env):
    env = repo_env
    env.installations = {
        1: ([installation(999)], 2),
        2: ([installation()], 3),
        3: ([installation(12)], None),
    }
    env.repositories = {
        (11, 1): ([], 2),
        (11, 2): ([repository(101), repository(102)], 3),
        (11, 3): ([], None),
        (12, 1): ([repository(103)], None),
    }
    ids, cursor = [], None
    while True:
        page = await env.service.discover(env.actor, 1, cursor)
        ids.extend(item.github_repository_id for item in page.items)
        cursor = page.next_cursor
        if cursor is None:
            break
    assert ids == [101, 102, 103]


@pytest.mark.asyncio
async def test_cursor_bound_to_actor_and_query(repo_env):
    env = repo_env
    env.repositories[(11, 1)] = ([repository(101), repository(102)], None)
    page = await env.service.discover(env.actor, 1, None)
    assert page.next_cursor
    other = Actor(uuid4(), 999, "admin", 1)
    for actor, limit in [(other, 1), (env.actor, 2)]:
        with pytest.raises(AppError) as error:
            await env.service.discover(actor, limit, page.next_cursor)
        assert error.value.status == 400
    env.settings.github_allowed_installation_ids = [11]
    with pytest.raises(AppError) as error:
        await env.service.discover(env.actor, 1, page.next_cursor)
    assert error.value.code == "invalid_cursor"


@pytest.mark.asyncio
async def test_discovery_filters_wrong_app_and_unverified_installations(repo_env):
    env = repo_env
    env.installations = {1: ([installation(11, app=8), installation(12)], None)}
    env.app_status = 404
    assert (await env.service.discover(env.actor, 20, None)).items == []


@pytest.mark.asyncio
async def test_discovery_rechecks_local_epochs_before_returning_collected_items(
    repo_env,
):
    from sqlalchemy import update

    from devflow.github.models import GitHubInstallation

    env = repo_env
    env.installations = {1: ([installation(11), installation(12)], None)}
    env.repositories[(12, 1)] = ([repository(103)], None)

    async def revoke_first(request):
        if request.url.path == "/user/installations/12/repositories":
            async with env.factory.begin() as session:
                await session.execute(
                    update(GitHubInstallation)
                    .where(GitHubInstallation.installation_id == 11)
                    .values(
                        available=False,
                        needs_verification=False,
                        auth_version=GitHubInstallation.auth_version + 1,
                    )
                )

    env.before_response = revoke_first
    page = await env.service.discover(env.actor, 20, None)
    assert [item.github_repository_id for item in page.items] == [103]
