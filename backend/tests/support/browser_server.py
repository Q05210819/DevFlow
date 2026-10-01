"""Owned acceptance API/Worker process; stdin EOF or 'stop' cleans its schema."""

import argparse
import asyncio
import json
import os
import signal
import sys
import tempfile
import threading
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
from support.full_flow import SyntheticGitHub, synthetic_settings


async def serve(settings, port):
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
    finally:
        await factory.kw["bind"].dispose()
    try:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(SyntheticGitHub())
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
    finally:
        for kind, handler in previous.items():
            signal.signal(kind, handler)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument("--origin", required=True)
    args = parser.parse_args()
    url = validated_test_url(
        os.environ.get(
            "TEST_DATABASE_URL",
            "postgresql+psycopg://devflow_test:devflow_test@127.0.0.1:55439/devflow_test",
        )
    )
    schema = "test_browser_" + uuid4().hex
    with psycopg.connect(
        url.set(drivername="postgresql").render_as_string(hide_password=False),
        autocommit=True,
    ) as connection:
        connection.execute(f'CREATE SCHEMA "{schema}"')
        try:
            print(json.dumps({"acceptance_schema": schema}), flush=True)
            with tempfile.TemporaryDirectory(prefix="devflow-browser-") as directory:
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
                asyncio.run(
                    serve(settings, args.port), loop_factory=asyncio.SelectorEventLoop
                )
        finally:
            connection.execute(f'DROP SCHEMA "{schema}" CASCADE')
            print(
                json.dumps({"acceptance_cleaned": True, "pid": os.getpid()}), flush=True
            )


if __name__ == "__main__":
    main()
