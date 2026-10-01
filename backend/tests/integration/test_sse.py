import asyncio
import json
from uuid import uuid4

import pytest
from sqlalchemy import delete, func, select, text, update
from support.job_env import job_env as job_env  # noqa: PLC0414
from support.job_env import repo_env as repo_env  # noqa: PLC0414
from support.repository_env import browser
from support.sse import append_status, stream, terminal_job

from devflow.auth.models import Session, User
from devflow.repositories.models import Repository

pytestmark = pytest.mark.asyncio


def event_data(body):
    lines = body.strip().splitlines()
    assert lines[1] == "event: job.status"
    payload = json.loads(lines[2].removeprefix("data: "))
    assert set(payload) == {"status", "attempt", "error_code"}
    return int(lines[0].removeprefix("id: ")), payload


@pytest.mark.parametrize(
    "denial", ["missing", "expired", "unknown", "other_admin", "revoked"]
)
async def test_sse_auth_before_headers_or_data(job_env, denial):
    env = job_env
    job = await terminal_job(env)
    async with browser(env) as (_, app, raw):
        job_id = job.id
        expected = 404
        if denial == "missing":
            raw, expected = None, 401
        elif denial == "expired":
            expected = 401
            async with env.factory.begin() as db:
                await db.execute(
                    update(Session).values(expires_at=func.clock_timestamp())
                )
        elif denial == "unknown":
            job_id = uuid4()
        elif denial == "other_admin":
            async with env.factory.begin() as db:
                other = User(github_id=999, role="admin")
                db.add(other)
                await db.flush()
                await db.execute(update(Session).values(user_id=other.id))
        else:
            async with env.factory.begin() as db:
                await db.execute(update(Repository).values(available=False))
        async with stream(app, f"/api/v1/jobs/{job_id}/events", raw) as response:
            assert response.status == expected
            assert response.headers["content-type"].startswith("application/json")
            data = json.loads(await response.body())
            assert data["error"]["code"] == (
                "unauthenticated" if expected == 401 else "resource_not_found"
            )
            assert str(job.id) not in json.dumps(data)


@pytest.mark.parametrize("after", ["0", "ignored-malformed"])
async def test_sse_replay_and_header_priority(job_env, after):
    env = job_env
    job = await terminal_job(env)
    async with (
        browser(env) as (_, app, raw),
        stream(
            app,
            f"/api/v1/jobs/{job.id}/events?after={after}",
            raw,
            {"Last-Event-ID": "2"},
        ) as response,
    ):
        assert response.status == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["x-accel-buffering"] == "no"
        received = [event_data(await response.body()) for _ in range(2)]
        assert [sequence for sequence, _ in received] == [3, 4]
        assert received[-1][1] == {
            "status": "failed",
            "attempt": 1,
            "error_code": None,
        }
        await response.finished()


@pytest.mark.parametrize(
    "cursor", ["-1", "abc", "1.5", "5", "", pytest.param("9" * 5000, id="oversized")]
)
@pytest.mark.parametrize("header", [False, True])
async def test_sse_rejects_invalid_cursor(job_env, cursor, header):
    env = job_env
    job = await terminal_job(env)
    async with browser(env) as (_, app, raw):
        query = "?after=0" if header else f"?after={cursor}"
        headers = {"Last-Event-ID": cursor} if header else None
        async with stream(
            app, f"/api/v1/jobs/{job.id}/events{query}", raw, headers
        ) as response:
            assert response.status == 400
            assert (
                json.loads(await response.body())["error"]["code"] == "invalid_cursor"
            )


async def test_terminal_cursor_closes_without_replay(job_env):
    env = job_env
    job = await terminal_job(env)
    async with (
        browser(env) as (_, app, raw),
        stream(app, f"/api/v1/jobs/{job.id}/events?after=4", raw) as response,
    ):
        assert response.status == 200
        await response.finished()


@pytest.mark.parametrize("loss", ["logout", "expired", "disabled", "repository"])
async def test_sse_local_revoke_and_expiry(job_env, monkeypatch, loss):
    from devflow.jobs import events

    async def tick(seconds):
        assert seconds == 1
        assert app.state.session_factory.kw["bind"].pool.checkedout() == 0

    monkeypatch.setattr(events, "sleep", tick)
    env = job_env
    job = await env.jobs.submit_probe(env.actor, env.repository.id, "sse")
    async with (
        browser(env) as (_, app, raw),
        stream(app, f"/api/v1/jobs/{job.id}/events", raw) as response,
    ):
        assert event_data(await response.body())[0] == 1
        assert app.state.session_factory.kw["bind"].pool.checkedout() == 0
        async with env.factory.begin() as db:
            if loss == "logout":
                await db.execute(delete(Session))
            elif loss == "expired":
                await db.execute(
                    update(Session).values(expires_at=func.clock_timestamp())
                )
            elif loss == "disabled":
                await db.execute(update(User).values(enabled=False))
            else:
                await db.execute(
                    update(Repository).values(
                        available=False, auth_version=Repository.auth_version + 1
                    )
                )
        await append_status(env, job.id, "running")
        assert await response.body() == "event: access_lost\ndata: {}\n\n"
        await response.finished()


@pytest.mark.parametrize("loss", ["session", "repository"])
async def test_rechecks_before_each_buffered_event(job_env, loss):
    env = job_env
    job = await terminal_job(env)
    async with (
        browser(env) as (_, app, raw),
        stream(app, f"/api/v1/jobs/{job.id}/events", raw) as response,
    ):
        assert event_data(await response.body())[0] == 1
        async with env.factory.begin() as db:
            if loss == "session":
                await db.execute(delete(Session))
            else:
                await db.execute(update(Repository).values(available=False))
        assert await response.body() == "event: access_lost\ndata: {}\n\n"
        await response.finished()


@pytest.mark.parametrize("status", [401, 404, 429, 503])
async def test_remote_expiry_revalidates_before_more_data(job_env, monkeypatch, status):
    from devflow.jobs import events

    env = job_env
    job = await env.jobs.submit_probe(env.actor, env.repository.id, "sse")

    async def tick(seconds):
        assert seconds == 1
        # Expire only the policy's monotonic positive cache. Session validity remains DB time.
        app.state.repository_policy._cache = dict.fromkeys(
            app.state.repository_policy._cache, 0
        )

    monkeypatch.setattr(events, "sleep", tick)
    async with (
        browser(env) as (_, app, raw),
        stream(app, f"/api/v1/jobs/{job.id}/events", raw) as response,
    ):
        assert event_data(await response.body())[0] == 1
        env.status = status
        await append_status(env, job.id, "running")
        if status in (401, 404):
            assert await response.body() == "event: access_lost\ndata: {}\n\n"
        await response.finished()


async def test_transient_initial_authorization_returns_503(job_env):
    env = job_env
    job = await terminal_job(env)
    env.status = 503
    async with (
        browser(env) as (_, app, raw),
        stream(app, f"/api/v1/jobs/{job.id}/events", raw) as response,
    ):
        assert response.status == 503
        assert (
            json.loads(await response.body())["error"]["code"] == "github_unavailable"
        )


async def test_recreated_api_replays_without_creating_jobs(job_env):
    from devflow.app import create_app
    from devflow.jobs.models import Job, RunEvent

    env = job_env
    job = await terminal_job(env)
    async with browser(env) as (_, app, raw):
        for application in (app, create_app(env.settings, github=env.github)):
            if application is app:
                async with stream(
                    application, f"/api/v1/jobs/{job.id}/events?after=2", raw
                ) as response:
                    assert [event_data(await response.body())[0] for _ in range(2)] == [
                        3,
                        4,
                    ]
                    await response.finished()
            else:
                async with (
                    application.router.lifespan_context(application),
                    stream(
                        application, f"/api/v1/jobs/{job.id}/events?after=2", raw
                    ) as response,
                ):
                    assert [event_data(await response.body())[0] for _ in range(2)] == [
                        3,
                        4,
                    ]
                    await response.finished()
    async with env.factory() as db:
        assert await db.scalar(select(func.count()).select_from(Job)) == 1
        assert await db.scalar(select(func.count()).select_from(RunEvent)) == 4


async def test_disconnect_does_not_cancel_job(job_env):
    from devflow.jobs.models import Job

    env = job_env
    job = await env.jobs.submit_probe(env.actor, env.repository.id, "sse")
    async with (
        browser(env) as (_, app, raw),
        stream(app, f"/api/v1/jobs/{job.id}/events", raw) as response,
    ):
        assert event_data(await response.body())[0] == 1
        response.disconnected.set()
        response.resume.set()
        await asyncio.wait_for(response.task, 5)
    async with env.factory() as db:
        row = await db.get(Job, job.id)
        assert row.status == "queued" and row.attempt == 0 and row.last_sequence == 1


async def test_heartbeat_every_15_seconds_and_poll_each_second(job_env, monkeypatch):
    from devflow.jobs import events

    env = job_env
    job = await env.jobs.submit_probe(env.actor, env.repository.id, "sse")
    elapsed = 0
    heartbeat_received = asyncio.Event()

    async def tick(seconds):
        nonlocal elapsed
        assert seconds == 1
        assert app.state.session_factory.kw["bind"].pool.checkedout() == 0
        if elapsed == 15:
            await heartbeat_received.wait()
        elapsed += seconds
        if elapsed == 16:
            await append_status(env, job.id, "succeeded")

    monkeypatch.setattr(events, "sleep", tick)
    monkeypatch.setattr(events, "monotonic", lambda: elapsed)
    async with (
        browser(env) as (_, app, raw),
        stream(app, f"/api/v1/jobs/{job.id}/events?after=1", raw) as response,
    ):
        assert await response.body() == ": heartbeat\n\n"
        assert elapsed == 15
        assert app.state.session_factory.kw["bind"].pool.checkedout() == 0
        heartbeat_received.set()
        sequence, data = event_data(await response.body())
        assert sequence == 2 and data["status"] == "succeeded"
        assert elapsed == 16
        await response.finished()


async def test_read_events_orders_limits_and_authorizes(job_env):
    from devflow.errors import AppError
    from devflow.jobs.events import JobEventService

    env = job_env
    job = await terminal_job(env)
    service = JobEventService(env.jobs)
    records = await service.read_events(env.actor, job.id, after=1, limit=2)
    assert [record.sequence for record in records] == [2, 3]
    assert records[1].status == "retry_wait"
    async with env.factory.begin() as db:
        await db.execute(update(Repository).values(available=False))
    with pytest.raises(AppError) as error:
        await service.read_events(env.actor, job.id, after=0)
    assert error.value.status == 404


async def test_remote_permissions_rechecked_at_60_seconds(job_env, monkeypatch):
    from types import SimpleNamespace

    from devflow.jobs import events
    from devflow.repositories import policy

    env = job_env
    job = await env.jobs.submit_probe(env.actor, env.repository.id, "sse")
    elapsed = 0
    first_received = asyncio.Event()
    checks = []

    async def tick(seconds):
        nonlocal elapsed
        await first_received.wait()
        assert seconds == 1
        elapsed += seconds

    async def observed_request(request):
        checks.append(elapsed)

    monkeypatch.setattr(events, "sleep", tick)
    monkeypatch.setattr(events, "monotonic", lambda: elapsed)
    # Replace the policy's scheduling clock alone; real session expiry still
    # uses PostgreSQL clock_timestamp and asyncio's own clock is unchanged.
    monkeypatch.setattr(policy, "time", SimpleNamespace(monotonic=lambda: elapsed))
    async with (
        browser(env) as (_, app, raw),
        stream(app, f"/api/v1/jobs/{job.id}/events", raw) as response,
    ):
        assert event_data(await response.body())[0] == 1
        env.before_response = observed_request
        env.status = 404
        first_received.set()
        assert [await response.body() for _ in range(3)] == [": heartbeat\n\n"] * 3
        assert await response.body() == "event: access_lost\ndata: {}\n\n"
        await response.finished()
        assert checks == [60]


@pytest.mark.parametrize("midstream", [False, True])
async def test_database_failure_is_transient(job_env, midstream):
    env = job_env
    job = await terminal_job(env)
    async with browser(env) as (_, app, raw):

        async def break_session_lookup():
            async with env.factory.begin() as db:
                await db.execute(
                    text("ALTER TABLE sessions RENAME TO unavailable_sessions")
                )

        if not midstream:
            await break_session_lookup()
        async with stream(app, f"/api/v1/jobs/{job.id}/events", raw) as response:
            if midstream:
                assert event_data(await response.body())[0] == 1
                await break_session_lookup()
                await response.finished()
            else:
                assert response.status == 503
                assert (
                    json.loads(await response.body())["error"]["code"]
                    == "service_unavailable"
                )


@pytest.mark.parametrize("midstream", [False, True])
async def test_logout_during_remote_verification_never_delivers_data(
    job_env, monkeypatch, midstream
):
    from devflow.jobs import events

    env = job_env
    job = await env.jobs.submit_probe(env.actor, env.repository.id, "sse")

    async def logout_during_response(request):
        if request.url.path.endswith("/repositories"):
            async with env.factory.begin() as db:
                await db.execute(delete(Session))

    async def tick(seconds):
        assert seconds == 1
        app.state.repository_policy._cache.clear()

    monkeypatch.setattr(events, "sleep", tick)
    async with browser(env) as (_, app, raw):
        if not midstream:
            env.before_response = logout_during_response
        async with stream(app, f"/api/v1/jobs/{job.id}/events", raw) as response:
            if midstream:
                assert event_data(await response.body())[0] == 1
                env.before_response = logout_during_response
                await append_status(env, job.id, "running")
                assert await response.body() == "event: access_lost\ndata: {}\n\n"
                await response.finished()
            else:
                assert response.status == 401
                assert (
                    json.loads(await response.body())["error"]["code"]
                    == "unauthenticated"
                )
