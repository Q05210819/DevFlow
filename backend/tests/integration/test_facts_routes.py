from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import select, update
from support.repository_env import browser
from support.repository_env import repo_env as repo_env  # noqa: PLC0414

from devflow.facts.models import Fact
from devflow.repositories.models import Repository

pytestmark = pytest.mark.asyncio


@pytest.fixture
def tmp_path(workspace_tmp_path):
    # Explicit R15 opt-in for new owned fixture files on Windows.
    return workspace_tmp_path


async def seed(env, count=31):
    repo = await env.service.register(env.actor, 11, 101)
    async with env.factory.begin() as db:
        for number in range(1, count + 1):
            db.add(
                Fact(
                    repository_id=repo.id,
                    kind="issue",
                    source_id=number,
                    number=number,
                    state="open",
                    source_updated_at=datetime(2026, 1, 1, tzinfo=UTC),
                    source_url=f"https://github.com/team/private/issues/{number}",
                    payload={"title": f"Issue {number}", "state": "open"},
                )
            )
    return repo


async def test_fact_pagination_limits_and_filter_binding(repo_env):
    env = repo_env
    repo = await seed(env, 101)
    path = f"/api/v1/repositories/{repo.id}/issues"
    async with browser(env) as (client, _app, raw):
        client.cookies.set("devflow_session", raw)
        first = await client.get(path)
        assert first.status_code == 200
        assert len(first.json()["items"]) == 30
        cursor = first.json()["next_cursor"]
        assert cursor
        second = await client.get(path, params={"cursor": cursor})
        assert second.status_code == 200
        assert not (
            {x["id"] for x in first.json()["items"]}
            & {x["id"] for x in second.json()["items"]}
        )
        assert (
            await client.get(path, params={"cursor": cursor, "state": "closed"})
        ).status_code == 400
        assert (
            len((await client.get(path, params={"limit": 100})).json()["items"]) == 100
        )
        assert (await client.get(path, params={"limit": 101})).status_code == 422
        assert first.headers["cache-control"] == "no-store"
        missing = await client.get(path + "/999")
        assert missing.json()["error"]["code"] == "fact_not_found"
        denied = await client.get(f"/api/v1/repositories/{uuid4()}/issues")
        assert denied.json()["error"]["code"] == "resource_not_found"


async def test_revocation_during_fact_read_blocks_response(repo_env, monkeypatch):
    env = repo_env
    repo = await seed(env)
    async with browser(env) as (client, app, raw):
        client.cookies.set("devflow_session", raw)
        policy = app.state.repository_policy
        original = policy.local
        calls = 0

        async def local(actor, identity):
            nonlocal calls
            calls += 1
            # Warm permission cache makes the read fence's last check deterministic.
            if calls == 3:
                async with env.factory.begin() as db:
                    await db.execute(
                        update(Repository)
                        .where(Repository.id == repo.id)
                        .values(enabled=False)
                    )
            return await original(actor, identity)

        await policy.require(env.actor, repo.id)
        monkeypatch.setattr(policy, "local", local)
        result = await client.get(f"/api/v1/repositories/{repo.id}/issues")
        assert calls == 3
        assert result.status_code == 404
        assert "items" not in result.json()


async def test_repository_detail_and_list_embed_sync(repo_env):
    env = repo_env
    repo = await seed(env, 0)
    async with browser(env) as (client, _app, raw):
        client.cookies.set("devflow_session", raw)
        detail = await client.get(f"/api/v1/repositories/{repo.id}")
        assert detail.status_code == 200
        assert detail.json()["sync"]["status"] == "queued"
        listed = await client.get("/api/v1/repositories")
        assert listed.json()["items"][0]["sync"]["requested_sequence"] == 1


async def test_collection_page_version_binding_and_safe_sources(repo_env):
    env = repo_env
    repo = await seed(env, 1)
    async with env.factory.begin() as db:
        for number in range(31):
            db.add(
                Fact(
                    repository_id=repo.id,
                    kind="comment",
                    parent_kind="issue",
                    parent_source_id=1,
                    source_id=number + 100,
                    source_url=f"https://api.github.com/repos/team/private/issues/comments/{number + 100}",
                    payload={"body": "comment"},
                )
            )
    path = f"/api/v1/repositories/{repo.id}/issues/1"
    async with browser(env) as (client, _app, raw):
        client.cookies.set("devflow_session", raw)
        detail = await client.get(path)
        assert detail.status_code == 200
        assert len(detail.json()["collections"]) == 1
        assert "items" not in detail.json()
        page = await client.get(path, params={"collection": "comments"})
        assert len(page.json()["items"]) == 30
        assert page.json()["items"][0]["source_url"].startswith(
            "https://github.com/team/private/issues/1#issuecomment-"
        )
        cursor = page.json()["next_cursor"]
        following = await client.get(
            path, params={"collection": "comments", "cursor": cursor}
        )
        assert len(following.json()["items"]) == 1
        async with env.factory.begin() as db:
            await db.execute(
                update(Fact)
                .where(Fact.repository_id == repo.id, Fact.kind == "issue")
                .values(
                    observed_at=datetime(2027, 1, 1, tzinfo=UTC),
                    source_url="https://github.com/other/private/issues/1",
                )
            )
        assert (
            await client.get(path, params={"collection": "comments", "cursor": cursor})
        ).status_code == 400
        assert (await client.get(path)).json()["item"]["source_url"] is None


async def test_cursor_rejects_other_actor_repository_and_page_size(repo_env):
    from support.repository_env import repository

    from devflow.auth.models import User
    from devflow.auth.types import Actor
    from devflow.errors import AppError
    from devflow.facts.service import FactService
    from devflow.github.models import GitHubCredential

    env = repo_env
    repo = await seed(env)
    env.repositories[(11, 1)] = ([repository(), repository(102, "team/second")], None)
    other_repo = await env.service.register(env.actor, 11, 102)
    async with env.factory.begin() as db:
        user = User(github_id=43, role="member")
        db.add(user)
        await db.flush()
        credential = await db.scalar(
            select(GitHubCredential).where(GitHubCredential.user_id == env.actor.id)
        )
        db.add(
            GitHubCredential(
                user_id=user.id,
                access_token_encrypted=credential.access_token_encrypted,
            )
        )
        other = Actor(user.id, user.github_id, user.role, user.auth_version)
    service = FactService(env.factory, env.policy, env.service.codec())
    first = await service.list(env.actor, repo.id, "issue")
    for actor, repository_id, limit in [
        (other, repo.id, 30),
        (env.actor, other_repo.id, 30),
        (env.actor, repo.id, 20),
    ]:
        with pytest.raises(AppError) as error:
            await service.list(
                actor, repository_id, "issue", cursor=first.next_cursor, limit=limit
            )
        assert error.value.code == "invalid_cursor"


async def test_later_page_database_failure_is_retryable(repo_env):
    from sqlalchemy import event
    from sqlalchemy.exc import OperationalError

    env = repo_env
    repo = await seed(env)
    path = f"/api/v1/repositories/{repo.id}/issues"
    async with browser(env) as (client, app, raw):
        assert (await client.get(path)).status_code == 401
        client.cookies.set("devflow_session", raw)
        first = (await client.get(path)).json()
        engine = app.state.session_factory.kw["bind"].sync_engine

        def fail(conn, cursor, statement, parameters, context, many):
            if "FROM facts" in statement:
                raise OperationalError("synthetic", {}, Exception())

        event.listen(engine, "before_cursor_execute", fail)
        try:
            failed = await client.get(path, params={"cursor": first["next_cursor"]})
            assert failed.status_code == 503
            assert "items" not in failed.json()
        finally:
            event.remove(engine, "before_cursor_execute", fail)
        retry = await client.get(path, params={"cursor": first["next_cursor"]})
        assert retry.status_code == 200 and len(retry.json()["items"]) == 1


async def test_actions_attempt_history_and_jobs_are_separate(repo_env):
    env = repo_env
    repo = await seed(env, 0)
    now = datetime.now(UTC)
    async with env.factory.begin() as db:
        for attempt, conclusion in [(1, "failure"), (2, "success")]:
            db.add(
                Fact(
                    repository_id=repo.id,
                    kind="ci_run",
                    source_id=10,
                    version_key=str(attempt),
                    source_url=f"https://github.com/team/private/actions/runs/10/attempts/{attempt}",
                    payload={
                        "attempt": attempt,
                        "workflow_id": 1,
                        "event": "push",
                        "head_sha": "a" * 40,
                        "status": "completed",
                        "conclusion": conclusion,
                        "created_at": now,
                        "updated_at": now,
                    },
                )
            )
            db.add(
                Fact(
                    repository_id=repo.id,
                    kind="ci_job",
                    source_id=attempt,
                    parent_kind="ci_run",
                    parent_source_id=10,
                    source_url=f"https://api.github.com/repos/team/private/actions/jobs/{attempt}",
                    payload={
                        "run_id": 10,
                        "attempt": attempt,
                        "head_sha": "a" * 40,
                        "name": "build",
                        "status": "completed",
                        "conclusion": conclusion,
                    },
                )
            )
    path = f"/api/v1/repositories/{repo.id}/actions/runs"
    async with browser(env) as (client, _app, raw):
        client.cookies.set("devflow_session", raw)
        page = await client.get(path)
        assert [r["payload"]["attempt"] for r in page.json()["items"]] == [2]
        old = await client.get(path + "/10/attempts/1", params={"collection": "jobs"})
        assert (
            old.json()["items"][0]["source_url"]
            == "https://github.com/team/private/actions/runs/10/job/1"
        )
        assert [r["payload"]["conclusion"] for r in old.json()["items"]] == ["failure"]
        history = await client.get(
            path + "/10/attempts/2", params={"collection": "attempts", "limit": 1}
        )
        assert history.json()["next_cursor"]
        replay = await client.get(
            path + "/10/attempts/1",
            params={
                "collection": "attempts",
                "limit": 1,
                "cursor": history.json()["next_cursor"],
            },
        )
        assert replay.status_code == 400
