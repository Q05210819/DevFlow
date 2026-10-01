"""Bounded ASGI streaming with backpressure; never buffers an infinite response."""

import asyncio
from contextlib import asynccontextmanager, suppress
from urllib.parse import urlsplit


class Stream:
    def __init__(self, app, path, raw=None, headers=None):
        url = urlsplit(path)
        self.app = app
        self.messages = asyncio.Queue()
        self.resume = asyncio.Event()
        self.disconnected = asyncio.Event()
        self.pending_body = False
        self.scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": url.path,
            "raw_path": url.path.encode(),
            "query_string": url.query.encode(),
            "root_path": "",
            "headers": [(b"host", b"localhost:3000")],
            "client": ("127.0.0.1", 1234),
            "server": ("localhost", 3000),
        }
        if raw is not None:
            self.scope["headers"].append((b"cookie", f"devflow_session={raw}".encode()))
        self.scope["headers"].extend(
            (key.lower().encode(), value.encode())
            for key, value in (headers or {}).items()
        )

    async def receive(self):
        await self.disconnected.wait()
        return {"type": "http.disconnect"}

    async def send(self, message):
        await self.messages.put(message)
        if message["type"] == "http.response.body" and message.get("more_body"):
            await self.resume.wait()
            self.resume.clear()

    async def next_message(self):
        if self.pending_body:
            self.resume.set()
        message = await asyncio.wait_for(self.messages.get(), timeout=5)
        self.pending_body = message["type"] == "http.response.body" and message.get(
            "more_body", False
        )
        return message

    async def body(self):
        message = await self.next_message()
        assert message["type"] == "http.response.body"
        return message.get("body", b"").decode()

    async def finished(self):
        assert await self.body() == ""
        assert not self.pending_body
        await asyncio.wait_for(self.task, timeout=5)


@asynccontextmanager
async def stream(app, path, raw=None, headers=None):
    connection = Stream(app, path, raw, headers)
    connection.task = asyncio.create_task(
        app(connection.scope, connection.receive, connection.send)
    )
    try:
        message = await connection.next_message()
        assert message["type"] == "http.response.start"
        connection.status = message["status"]
        connection.headers = {
            key.decode(): value.decode() for key, value in message["headers"]
        }
        yield connection
    finally:
        connection.disconnected.set()
        connection.resume.set()
        if not connection.task.done():
            connection.task.cancel()
        with suppress(asyncio.CancelledError):
            await connection.task


async def append_status(env, job_id, status, attempt=1):
    from sqlalchemy import select

    from devflow.jobs.models import Job
    from devflow.jobs.service import append_event

    async with env.factory.begin() as session:
        job = await session.scalar(
            select(Job).where(Job.id == job_id).with_for_update()
        )
        job.status, job.attempt = status, attempt
        await append_event(session, job, status)


async def terminal_job(env):
    job = await env.jobs.submit_probe(env.actor, env.repository.id, "sse")
    for status in ("running", "retry_wait", "failed"):
        await append_status(env, job.id, status)
    return job
