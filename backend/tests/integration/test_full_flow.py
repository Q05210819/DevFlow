"""Cross-module acceptance uses the actual app, PostgreSQL and normal Worker."""

import asyncio
import json
import os
import socket
import subprocess
import sys
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import psycopg
import pytest
from conftest import validated_test_url
from sqlalchemy import select
from support.full_flow import (
    ADMIN_ID,
    INSTALLATION_ID,
    MEMBER_ID,
    REPOSITORY_ID,
    REPOSITORY_NAME,
    SyntheticGitHub,
    csrf_headers,
    oauth_login,
    synthetic_settings,
)

from devflow.app import create_app
from devflow.audit.models import AuditLog
from devflow.bootstrap import bootstrap_admin
from devflow.github.client import GitHubClient
from devflow.jobs.models import RunEvent


@pytest.mark.parametrize(
    ("origin", "fails"),
    [("http://invalid.example", True), ("http://localhost:3000", False)],
    ids=["settings-failure", "parent-stdin-closed"],
)
def test_browser_helper_cleans_own_schema_on_setup_failure_or_parent_exit(
    origin, fails
):
    url = validated_test_url(
        os.environ.get(
            "TEST_DATABASE_URL",
            "postgresql+psycopg://devflow_test:devflow_test@127.0.0.1:55439/devflow_test",
        )
    )
    helper = Path(__file__).parents[1] / "support" / "browser_server.py"
    # Reject invalid settings or simulate a disappearing parent by closing stdin
    # at startup. Both paths must remove their real, exclusively owned schema.
    process = subprocess.run(
        [
            sys.executable,
            str(helper),
            "--port",
            "0",
            "--origin",
            origin,
        ],
        capture_output=True,
        check=False,
        input="",
        text=True,
        timeout=20,
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
    )
    assert (process.returncode != 0) is fails, process.stderr
    records = [
        json.loads(line) for line in process.stdout.splitlines() if line.startswith("{")
    ]
    schema = next(
        record["acceptance_schema"]
        for record in records
        if "acceptance_schema" in record
    )
    assert any(record.get("acceptance_cleaned") for record in records)
    with psycopg.connect(
        url.set(drivername="postgresql").render_as_string(hide_password=False)
    ) as connection:
        assert (
            connection.execute("SELECT to_regnamespace(%s)", (schema,)).fetchone()[0]
            is None
        )


@pytest.mark.asyncio
async def test_allowlist_oauth_repository_worker_events_audit_and_revocation(
    migrated_database, tmp_path
):
    # These are reviewed dependency interfaces, never replacement test workers.
    from support.sse import stream

    from devflow.worker import run_worker

    settings = synthetic_settings(
        tmp_path,
        migrated_database[0].database_url.get_secret_value(),
        "http://localhost:3000",
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(SyntheticGitHub())
    ) as provider:
        github = GitHubClient(provider, settings)
        app = create_app(settings, github=github)
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url=settings.public_base_url,
            ) as admin,
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url=settings.public_base_url,
            ) as member,
        ):
            await bootstrap_admin(app.state.session_factory, ADMIN_ID)
            admin_session = await oauth_login(admin, "admin")
            headers = csrf_headers(admin_session, settings.public_base_url)
            allowed = await admin.post(
                "/api/v1/admin/users",
                headers=headers,
                json={"github_id": MEMBER_ID, "role": "member"},
            )
            assert allowed.status_code == 201, allowed.text
            member_id = allowed.json()["id"]
            member_session = await oauth_login(member, "member")
            assert member_session["user"]["id"] == member_id
            discovery = await admin.get("/api/v1/github/repositories")
            assert discovery.status_code == 200, discovery.text
            candidate = discovery.json()["items"][0]
            assert candidate["github_repository_id"] == REPOSITORY_ID
            registered = await admin.post(
                "/api/v1/repositories",
                headers=headers,
                json={
                    "installation_id": INSTALLATION_ID,
                    "github_repository_id": candidate["github_repository_id"],
                },
            )
            assert registered.status_code == 201, registered.text
            repository_id = registered.json()["id"]
            submitted = await member.post(
                f"/api/v1/repositories/{repository_id}/access-checks",
                headers={
                    **csrf_headers(member_session, settings.public_base_url),
                    "Idempotency-Key": str(uuid4()),
                },
            )
            assert submitted.status_code == 202, submitted.text
            job_id = submitted.json()["id"]
            assert submitted.json()["status"] == "queued"
            stop = asyncio.Event()
            worker = asyncio.create_task(run_worker(settings, stop=stop, github=github))
            try:
                events = []
                async with (
                    asyncio.timeout(15),
                    stream(
                        app,
                        f"/api/v1/jobs/{job_id}/events",
                        member.cookies.get("devflow_session"),
                    ) as connection,
                ):
                    assert connection.status == 200
                    while len(events) < 3:
                        chunk = await connection.body()
                        if chunk.startswith(":"):
                            continue
                        fields = dict(
                            line.split(":", 1)
                            for line in chunk.strip().splitlines()
                            if ":" in line
                        )
                        assert fields["event"].strip() == "job.status"
                        events.append((int(fields["id"]), json.loads(fields["data"])))
                    await connection.finished()
                assert [seq for seq, _ in events] == [1, 2, 3]
                assert [event["status"] for _, event in events] == [
                    "queued",
                    "running",
                    "succeeded",
                ]
                detail = await member.get(f"/api/v1/jobs/{job_id}")
                assert detail.status_code == 200, detail.text
                job = detail.json()
                assert job["status"] == "succeeded"
                assert job["repository_id"] == repository_id
                assert job["requested_by"] == member_id
                assert job["attempt"] == 1
                assert job["last_sequence"] == 3
                assert job["result"]["github_repository_id"] == REPOSITORY_ID
                assert job["result"]["full_name"] == REPOSITORY_NAME
                assert job["result"]["private"] is True
                assert job["result"]["checked_at"]
                async with app.state.session_factory() as database:
                    persisted = (
                        await database.scalars(
                            select(RunEvent)
                            .where(RunEvent.job_id == UUID(job_id))
                            .order_by(RunEvent.sequence)
                        )
                    ).all()
                    assert [
                        (event.sequence, event.payload) for event in persisted
                    ] == events
                    audits = (await database.scalars(select(AuditLog))).all()
                    assert {
                        "bootstrap.admin",
                        "member.created",
                        "auth.login",
                        "repository.registered",
                        "job.submitted",
                        "job.started",
                        "job.succeeded",
                    } <= {entry.action for entry in audits}
                    assert all(
                        entry.job_id == UUID(job_id)
                        for entry in audits
                        if entry.action.startswith("job.")
                    )
                # Even an admin cannot read another creator's private run.
                assert (await admin.get(f"/api/v1/jobs/{job_id}")).status_code == 404
                revoked = await admin.patch(
                    f"/api/v1/admin/users/{member_id}",
                    headers=headers,
                    json={"enabled": False},
                )
                assert revoked.status_code == 200, revoked.text
                hidden = await member.get(f"/api/v1/jobs/{job_id}")
                assert hidden.status_code == 401
                assert REPOSITORY_NAME not in hidden.text
                assert "result" not in hidden.json()
            finally:
                stop.set()
                await asyncio.wait_for(worker, timeout=35)


@pytest.mark.asyncio
async def test_protected_rest_database_outage_is_503_before_job_route(tmp_path):
    # Reserve a non-listening loopback socket. No database or foreign service is
    # stopped; connecting to this owned endpoint deterministically fails.
    with socket.socket() as unavailable:
        unavailable.bind(("127.0.0.1", 0))
        port = unavailable.getsockname()[1]
        settings = synthetic_settings(
            tmp_path,
            f"postgresql+psycopg://synthetic:synthetic@127.0.0.1:{port}/unused",
            "http://localhost:3000",
        )
        app = create_app(settings)
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url=settings.public_base_url,
                cookies={"devflow_session": "synthetic-session-needs-database"},
            ) as client,
        ):
            response = await client.get(f"/api/v1/jobs/{uuid4()}")
        assert response.status_code == 503, response.text
        assert response.json()["error"]["code"] == "service_unavailable"
        assert "synthetic" not in response.text
        assert "connection" not in response.text.lower()


@pytest.mark.asyncio
async def test_programming_error_remains_500(settings_factory):
    app = create_app(settings_factory())

    @app.get("/acceptance-programming-error")
    async def broken():
        raise RuntimeError("private-programming-error")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost:3000"
    ) as client:
        response = await client.get("/acceptance-programming-error")
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_error"
    assert "private-programming-error" not in response.text
