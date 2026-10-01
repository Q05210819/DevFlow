"""Owned subprocess harness. Only GitHub's HTTP transport is synthetic."""

import asyncio
import json
import sys
from pathlib import Path

import httpx

from devflow.config import Settings
from devflow.github.client import GitHubClient
from devflow.worker import run_worker


async def main():
    config_path, marker_path, stop_path, mode = sys.argv[1:]
    marker, stopping = Path(marker_path), Path(stop_path)
    settings = Settings(_env_file=None, **json.loads(Path(config_path).read_text()))
    stop = asyncio.Event()

    async def remote(request):
        assert request.method == "GET"
        assert request.url.host == "api.github.com"
        installation = {
            "id": 11,
            "app_id": 7,
            "account": {"id": 99},
            "suspended_at": None,
        }
        if request.url.path == "/user/installations":
            return httpx.Response(200, json={"installations": [installation]})
        if request.url.path == "/app/installations/11":
            return httpx.Response(200, json=installation)
        assert request.url.path == "/user/installations/11/repositories"
        marker.write_text("provider-request-started")
        if mode == "blocked":
            await asyncio.Event().wait()
        return httpx.Response(
            200,
            json={
                "repositories": [
                    {
                        "id": 101,
                        "full_name": "team/private",
                        "private": True,
                        "default_branch": "main",
                    }
                ]
            },
        )

    async def watch_stop():
        while not stopping.exists():
            await asyncio.sleep(0.05)
        stop.set()

    watcher = asyncio.create_task(watch_stop())
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(remote)) as http:
            await run_worker(settings, stop=stop, github=GitHubClient(http, settings))
    finally:
        watcher.cancel()
        await asyncio.gather(watcher, return_exceptions=True)


if __name__ == "__main__":
    asyncio.run(main(), loop_factory=asyncio.SelectorEventLoop)
