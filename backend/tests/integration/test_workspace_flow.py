"""Real API/ordinary Worker/PostgreSQL, with only provider and transfer synthetic."""

import asyncio
import json
import os
import time
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from conftest import validated_test_url
from psycopg import sql
from sqlalchemy import select
from support.full_flow import REPOSITORY_ID, WorkspaceFixture
from test_data_sync_flow import (
    completed,
    data_flow,  # noqa: F401 - shared pytest fixture
    webhook,
    worker,
)

from devflow.jobs.models import Job
from devflow.workspace.models import WorkspaceVersion

pytestmark = pytest.mark.asyncio


@pytest.fixture
def tmp_path(workspace_tmp_path):
    return workspace_tmp_path


@pytest.fixture
def migrated_database(settings_factory, tmp_path):
    """Keep this flow's schema if physical cleanup is not positively recorded."""
    url = validated_test_url(
        os.environ.get(
            "TEST_DATABASE_URL",
            "postgresql+psycopg://devflow_test:devflow_test@127.0.0.1:55439/devflow_test",
        )
    )
    schema = "test_workspace_flow_" + uuid4().hex
    evidence = tmp_path / "database-owner.json"
    evidence.write_text(
        json.dumps({"schema": schema, "cleaned": False}), encoding="utf-8"
    )
    with psycopg.connect(
        url.set(drivername="postgresql").render_as_string(hide_password=False),
        autocommit=True,
    ) as conn:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        try:
            scoped = url.update_query_dict({"options": f"-csearch_path={schema}"})
            settings = settings_factory(
                database_url=scoped.render_as_string(hide_password=False)
            )
            config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
            config.attributes["database_url"] = settings.database_url.get_secret_value()
            command.upgrade(config, "head")
            yield settings, config
        finally:
            table = conn.execute(
                "SELECT to_regclass(%s)", (schema + ".workspace_process_guards",)
            ).fetchone()[0]
            unresolved = (
                []
                if table is None
                else conn.execute(
                    sql.SQL(
                        "SELECT operation_id,state FROM {}.workspace_process_guards WHERE state <> 'drained'"
                    ).format(sql.Identifier(schema))
                ).fetchall()
            )
            if not unresolved:
                conn.execute(
                    sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema))
                )
            cleaned = (
                conn.execute("SELECT to_regnamespace(%s)", (schema,)).fetchone()[0]
                is None
            )
            evidence.write_text(
                json.dumps(
                    {"schema": schema, "cleaned": cleaned, "unresolved": unresolved}
                ),
                encoding="utf-8",
            )
            print("workspace_database_cleanup", schema, cleaned)


@pytest_asyncio.fixture
async def workspace_flow(data_flow, tmp_path):  # noqa: F811 - shared pytest fixture
    env = data_flow
    env.objects = WorkspaceFixture(tmp_path)
    env.remote.workspace = env.objects
    await env.objects.record_guards(env.factory)
    with env.objects.transfer_context():
        try:
            yield env
        finally:
            print("synthetic_workspace_requests", env.remote.workspace_requests[-20:])
            guards = await env.objects.record_guards(env.factory)
            assert all(row["state"] == "drained" for row in guards), guards
            assert all(
                row["proof"] in {"physical", "never_activated"} for row in guards
            )


async def prepare(env, selection, key=None):
    response = await env.member.post(
        env.path + "/workspaces",
        headers={**env.headers, "Idempotency-Key": key or str(uuid4())},
        json=selection,
    )
    assert response.status_code == 202, response.text
    assert response.headers["cache-control"] == "no-store"
    identity = response.json()["id"]
    async with asyncio.timeout(45):
        while True:
            response = await env.member.get(env.path + "/workspaces/" + identity)
            assert response.status_code == 200, response.text
            view = response.json()
            assert view["status"] not in {"failed", "unavailable"}, view
            if view["status"] == "ready":
                return view
            await asyncio.sleep(0.1)


async def read_file(env, view):
    response = await env.member.get(
        env.path + "/workspaces/" + view["id"] + "/file",
        params={"path": "src/example.py"},
    )
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    return response.json()


async def test_real_worker_fixed_pr_force_update_restart_and_revocation(workspace_flow):
    env = workspace_flow
    selection = {"kind": "pull", "number": 2, "side": "head"}
    key = str(uuid4())
    async with worker(env):
        await completed(env)
        first = await prepare(env, selection, key)
        assert first["commit_sha"] == env.objects.commits[0]
        file = await read_file(env, first)
        assert file["text"] == "version A\r\nneedle A\n"
        assert (file["start_line"], file["end_line"]) == (1, 2)
        assert file["source_url"].endswith("/src/example.py#L1-L2")
        tree = await env.member.get(
            env.path + "/workspaces/" + first["id"] + "/tree", params={"path": "src"}
        )
        assert tree.status_code == 200, tree.text
        assert tree.json()["items"][0]["path"] == "src/example.py"
        search = await env.member.get(
            env.path + "/workspaces/" + first["id"] + "/search",
            params={"query": "needle"},
        )
        assert search.status_code == 200, search.text
        hit = search.json()["items"][0]
        assert hit["text"] == "needle A\n"
        assert hit["citation"]["commit_sha"] == first["commit_sha"]
        assert hit["citation"]["start_line"] == 2
    # Ordinary Worker stop/restart uses the same DB/cache, not manual prepare.
    env.objects.advance(1)
    async with worker(env):
        second = await prepare(env, selection)
        assert second["id"] != first["id"]
        assert second["commit_sha"] == env.objects.commits[1]
        assert (await read_file(env, second))["text"] == "version B\r\nneedle B\n"
        assert (await read_file(env, first))["text"] == "version A\r\nneedle A\n"
        replay = await prepare(env, selection, key)
        assert replay["id"] == first["id"]
        assert replay["commit_sha"] == first["commit_sha"]
        async with env.factory() as db:
            versions = list(await db.scalars(select(WorkspaceVersion)))
            jobs = list(
                await db.scalars(select(Job).where(Job.kind == "repository.workspace"))
            )
            assert len(versions) == len(jobs) == 2
            assert all(job.status == "succeeded" for job in jobs)
            assert {job.result["workspace_id"] for job in jobs} == {
                first["id"],
                second["id"],
            }
            assert all(
                (env.objects.root / f"{REPOSITORY_ID}.git" / row.pin_ref)
                .read_text()
                .strip()
                == row.commit_sha
                for row in versions
            )
        await webhook(
            env,
            "installation_repositories",
            action="removed",
            repositories_removed=[{"id": REPOSITORY_ID}],
        )
        for suffix in (
            "",
            "/tree",
            "/file?path=src%2Fexample.py",
            "/search?query=needle",
        ):
            denied = await env.member.get(
                env.path + "/workspaces/" + first["id"] + suffix
            )
            assert denied.status_code == 404, denied.text
            assert denied.headers["cache-control"] == "no-store"
            assert "needle A" not in denied.text
    guards = await env.objects.record_guards(env.factory)
    write_ids = {item["operation_id"] for item in env.objects.transfers}
    assert len(write_ids) == 2
    assert all(
        row["proof"] == "physical" and row["identity"]
        for row in guards
        if row["operation_id"] in write_ids
    )


async def test_real_worker_tag_ci_and_missing_object_are_explicit(
    workspace_flow, monkeypatch
):
    env = workspace_flow
    from devflow.workspace.git import GitCache
    from devflow.workspace.process import WorkspaceProcessError

    operation = GitCache.operation
    started = time.monotonic()

    @contextmanager
    def observed(cache, guard, **options):
        assert cache.root.resolve() == env.objects.root

        def stage(name):
            env.objects.stages.append(
                {
                    "operation_id": guard.operation_id,
                    "kind": guard.kind,
                    "stage": name,
                    "elapsed": round(time.monotonic() - started, 3),
                    "remaining": round(options["deadline"] - time.monotonic(), 3),
                }
            )

        stage("start_reserved")
        try:
            with operation(cache, guard, **options) as native:
                stage("active_ready")
                yield native
        except WorkspaceProcessError as error:
            stage(str(error))
            raise
        finally:
            stage("closed")

    monkeypatch.setattr(GitCache, "operation", observed)
    async with worker(env):
        await completed(env)
        for selection in (
            {"kind": "branch", "name": "main"},
            {"kind": "tag", "name": "v1"},
            {"kind": "tag", "name": "light"},
            {"kind": "ci", "run_id": 81001, "attempt": 2},
        ):
            view = await prepare(env, selection)
            assert view["commit_sha"] == env.objects.commits[0]
            assert (await read_file(env, view))["text"] == "version A\r\nneedle A\n"
    # Delete only the positively owned loose commit after the Worker has joined.
    sha = env.objects.commits[0]
    missing = env.objects.root / f"{REPOSITORY_ID}.git" / "objects" / sha[:2] / sha[2:]
    assert missing.resolve().is_relative_to(env.objects.root.resolve())
    missing.unlink()
    response = await env.member.get(env.path + "/workspaces/" + view["id"] + "/tree")
    assert response.status_code == 503, response.text
    assert response.json()["error"]["code"] == "git_failed"
    assert response.headers["cache-control"] == "no-store"
    assert "items" not in response.json()
    # Missing objects do not silently replace an immutable SHA with a newer ref.
    async with env.factory() as db:
        row = await db.get(WorkspaceVersion, UUID(view["id"]))
        assert row.commit_sha == sha
