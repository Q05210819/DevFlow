"""Owned acceptance API/Worker process; stdin EOF or 'stop' cleans its schema."""

import argparse
import asyncio
import json
import os
import shutil
import signal
import sys
import threading
from contextlib import nullcontext
from pathlib import Path
from uuid import uuid4

import httpx
import psycopg
import uvicorn
from alembic import command
from alembic.config import Config

# The standalone helper uses the same test-DB guard as pytest, never the user's DB.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from conftest import validated_test_url

from devflow.app import create_app
from devflow.bootstrap import bootstrap_admin
from devflow.db import create_session_factory
from devflow.github.client import GitHubClient
from support.full_flow import SyntheticGitHub, WorkspaceFixture, synthetic_settings


async def serve(settings, port, workspace=None):
    from devflow.worker import run_worker

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()

    def request_stop(*_args):
        loop.call_soon_threadsafe(stop.set)

    def monitor_parent():
        # A closed pipe also cleans up after a failed/interrupted Playwright worker.
        sys.stdin.readline()
        request_stop()

    threading.Thread(target=monitor_parent, daemon=True).start()
    previous = {
        kind: signal.signal(kind, request_stop)
        for kind in (signal.SIGINT, signal.SIGTERM)
    }
    factory = create_session_factory(settings)
    try:
        await bootstrap_admin(factory, settings.bootstrap_admin_github_id)
        if workspace is not None:
            await workspace.record_guards(factory)
    finally:
        await factory.kw["bind"].dispose()
    try:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(SyntheticGitHub(workspace))
        ) as http:
            github = GitHubClient(http, settings)
            app = create_app(settings, github=github)
            server = uvicorn.Server(
                uvicorn.Config(
                    app,
                    host="127.0.0.1",
                    port=port,
                    access_log=False,
                    log_level="warning",
                )
            )
            worker = asyncio.create_task(run_worker(settings, stop=stop, github=github))
            api = asyncio.create_task(server.serve())
            waiter = asyncio.create_task(stop.wait())
            try:
                while not server.started:
                    if api.done():
                        await api
                        raise RuntimeError("Acceptance API stopped before readiness")
                    if worker.done():
                        await worker
                        raise RuntimeError("Acceptance Worker stopped before readiness")
                    if stop.is_set():
                        return
                    await asyncio.sleep(0.05)
                print(
                    json.dumps(
                        {"acceptance_ready": True, "pid": os.getpid(), "port": port}
                    ),
                    flush=True,
                )
                await asyncio.wait(
                    {api, worker, waiter}, return_when=asyncio.FIRST_COMPLETED
                )
            finally:
                stop.set()
                server.should_exit = True
                waiter.cancel()
                await asyncio.gather(waiter, return_exceptions=True)
                await asyncio.wait_for(asyncio.gather(api, worker), timeout=40)
                if workspace is not None:
                    audit_factory = create_session_factory(settings)
                    try:
                        guards = await workspace.record_guards(audit_factory)
                        assert all(row["state"] == "drained" for row in guards), (
                            "Workspace cleanup remains unresolved"
                        )
                    finally:
                        await audit_factory.kw["bind"].dispose()
    finally:
        for kind, handler in previous.items():
            signal.signal(kind, handler)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument("--origin", required=True)
    parser.add_argument("--scenario", choices=("facts", "workspace"), default="facts")
    args = parser.parse_args()
    url = validated_test_url(
        os.environ.get(
            "TEST_DATABASE_URL",
            "postgresql+psycopg://devflow_test:devflow_test@127.0.0.1:55439/devflow_test",
        )
    )
    schema = "test_browser_" + uuid4().hex
    workspace = None
    runtime_started = False
    runtime_cleaned = False
    fixture_root = (
        Path(__file__).resolve().parents[3] / ".superpowers" / "workspace-fixtures"
    )
    fixture_root.mkdir(parents=True, exist_ok=True)
    owned = fixture_root / uuid4().hex
    owned.mkdir(mode=0o777 if os.name == "nt" else 0o700)
    print(
        json.dumps({"acceptance_fixture": str(owned), "pid": os.getpid()}), flush=True
    )
    with psycopg.connect(
        url.set(drivername="postgresql").render_as_string(hide_password=False),
        autocommit=True,
    ) as connection:
        connection.execute(f'CREATE SCHEMA "{schema}"')
        try:
            print(json.dumps({"acceptance_schema": schema}), flush=True)
            if args.scenario == "workspace":
                workspace = WorkspaceFixture(owned)
            with nullcontext(str(owned)) as directory:
                scoped = url.update_query_dict({"options": f"-csearch_path={schema}"})
                settings = synthetic_settings(
                    Path(directory),
                    scoped.render_as_string(hide_password=False),
                    args.origin,
                )
                config = Config(
                    str(Path(__file__).resolve().parents[2] / "alembic.ini")
                )
                config.attributes["database_url"] = (
                    settings.database_url.get_secret_value()
                )
                command.upgrade(config, "head")
                with workspace.transfer_context() if workspace else nullcontext():
                    runtime_started = True
                    asyncio.run(
                        serve(settings, args.port, workspace),
                        loop_factory=asyncio.SelectorEventLoop,
                    )
                    runtime_cleaned = True
        finally:
            # An uncertain Workspace teardown retains DB + owned files for proof.
            cleaned = workspace is None or not runtime_started or runtime_cleaned
            if cleaned:
                connection.execute(f'DROP SCHEMA "{schema}" CASCADE')
                assert (
                    connection.execute(
                        "SELECT to_regnamespace(%s)", (schema,)
                    ).fetchone()[0]
                    is None
                )
                if workspace is None:
                    assert owned.resolve().parent == fixture_root.resolve()
                    assert not owned.is_symlink() and not owned.is_junction()
                    shutil.rmtree(owned)
            print(
                json.dumps({"acceptance_cleaned": cleaned, "pid": os.getpid()}),
                flush=True,
            )


if __name__ == "__main__":
    main()
