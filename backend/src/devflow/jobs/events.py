"""Replay committed events with short-lived sessions and revocable authorization."""

from asyncio import sleep
from collections.abc import AsyncIterator
from time import monotonic
from uuid import UUID

from fastapi import Request
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from devflow.auth.dependencies import require_actor
from devflow.auth.service import unauthenticated
from devflow.auth.types import Actor
from devflow.errors import AppError
from devflow.github.client import not_found
from devflow.jobs.models import Job, RunEvent
from devflow.jobs.schemas import EventView
from devflow.jobs.service import JobService

TERMINAL = {"succeeded", "failed"}


def parse_cursor(raw: str, maximum: int) -> int:
    # Bound conversion before int(): arbitrarily large client input is still a 400.
    if not raw or len(raw) > 19 or not raw.isascii() or not raw.isdecimal():
        raise AppError("invalid_cursor", 400, "Invalid event cursor")
    after = int(raw)
    if after > maximum:
        raise AppError("invalid_cursor", 400, "Invalid event cursor")
    return after


class JobEventService:
    def __init__(self, jobs: JobService):
        self.jobs = jobs

    async def read_events(
        self, actor: Actor, job_id: UUID, after: int, limit: int = 100
    ) -> list[EventView]:
        _, snapshots = await self.jobs.visibility(actor, job_id)
        # An epoch may change between the first authorization and this snapshot.
        # Require these exact snapshots before reading, and fence them under the
        # same short transaction used by ordinary job reads.
        current = await self.jobs._authorize_all(
            actor, [key[1] for key in snapshots], fresh=False
        )
        if current != snapshots:
            raise not_found()
        async with self.jobs.factory.begin() as session:
            await self.jobs._lock_authorizations(session, actor, snapshots)
            rows = await session.scalars(
                select(RunEvent)
                .join(Job, Job.id == RunEvent.job_id)
                .where(
                    Job.id == job_id,
                    Job.requested_by == actor.id,
                    RunEvent.sequence > after,
                )
                .order_by(RunEvent.sequence)
                .limit(max(1, min(limit, 100)))
            )
            return [EventView(sequence=row.sequence, **row.payload) for row in rows]

    async def _authorize(self, request: Request, actor: Actor, job_id: UUID):
        current = await require_actor(request)
        if current != actor:
            raise unauthenticated()
        job, snapshots = await self.jobs.visibility(actor, job_id)
        # A remote check can take time: logout/expiry during it must also deny.
        if await require_actor(request) != actor:
            raise unauthenticated()
        return job, snapshots

    async def stream_job_events(
        self, request: Request, actor: Actor, job_id: UUID, after: int
    ) -> AsyncIterator[str]:
        heartbeat_at = monotonic()
        try:
            while not await request.is_disconnected():
                job, snapshot = await self._authorize(request, actor, job_id)
                batch = await self.read_events(actor, job_id, after)
                for event in batch:
                    if await request.is_disconnected():
                        return
                    _, current = await self._authorize(request, actor, job_id)
                    if current != snapshot:
                        raise not_found()
                    # All database contexts have exited before yielding bytes.
                    payload = event.model_dump_json(exclude={"sequence"})
                    yield f"id: {event.sequence}\nevent: job.status\ndata: {payload}\n\n"
                    after = event.sequence
                    if event.status in TERMINAL:
                        return
                if job.status in TERMINAL and after >= job.last_sequence:
                    return
                if monotonic() - heartbeat_at >= 15:
                    await self._authorize(request, actor, job_id)
                    yield ": heartbeat\n\n"
                    heartbeat_at = monotonic()
                await sleep(1)
        except AppError as error:
            if error.status in (401, 403, 404):
                yield "event: access_lost\ndata: {}\n\n"
            # A dependency outage is not permanent permission loss. Close and
            # let the browser's REST probe classify it and choose bounded retry.
        except SQLAlchemyError:
            return
