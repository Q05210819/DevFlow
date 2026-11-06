"""Real finite chat handlers; the Job transition belongs to the handler.

``ChatRunHandler`` executes one claimed ``chat.run`` Job and publishes its
terminal transition itself. ``run`` deliberately returns ``None``, so the
worker never applies the generic ``queue.complete`` path to a chat run.
"""

from uuid import UUID

from devflow.chat.retention import ChatRetention
from devflow.chat.service import ChatService
from devflow.errors import AppError
from devflow.jobs.queue import Queue
from devflow.sync.scope import transaction


class ChatCleanupHandler:
    """Drive one bounded terminal-thread sweep under its own lease."""

    def __init__(self, retention: ChatRetention):
        self.retention = retention

    async def run(self, lease) -> None:
        await self.retention.sweep(lease)


class ChatRunHandler:
    """Run one durable chat graph attempt, then finish the run and Job."""

    def __init__(self, graph, factory, policy):
        self.graph = graph
        self.factory = factory
        self.policy = policy

    async def run(self, lease) -> None:
        run_id = await self._run_id(lease)
        outcome = await self.graph.execute(lease, run_id)
        async with transaction(self.factory) as db:
            await ChatService(self.factory, self.policy).finish(
                db, lease, run_id, outcome
            )

    async def _run_id(self, lease) -> UUID:
        async with transaction(self.factory) as db:
            job, _ = await Queue(self.factory)._owned(db, lease)
            if job is None:
                raise AppError("lease_expired", 409, "Chat run lease expired")
            if job.kind != "chat.run":
                raise AppError("internal_error", 500, "Job is not a chat run")
            payload = job.input if isinstance(job.input, dict) else None
            try:
                if payload is None or payload.get("version") != 1:
                    raise ValueError("Chat run input version is invalid")
                return UUID(payload["run_id"])
            except (KeyError, TypeError, ValueError):
                raise AppError(
                    "internal_error", 500, "Chat run input is invalid"
                ) from None
