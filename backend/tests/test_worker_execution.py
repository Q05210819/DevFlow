"""Scheduling tests; SQL fencing itself is exercised against PostgreSQL."""

import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest


class RecordingQueue:
    def __init__(self, *, owns=True):
        self.owns = owns
        self.heartbeats = 0
        self.writes = []

    async def heartbeat(self, lease):
        self.heartbeats += 1
        return self.owns

    async def complete(self, lease, result):
        self.writes.append(("complete", result))
        return True

    async def fail(self, lease, code, retryable, retry_at=None):
        self.writes.append(("fail", code, retryable, retry_at))
        return True


def lease():
    return SimpleNamespace(job_id=uuid4(), owner=uuid4(), generation=1, attempt=1)


@pytest.mark.asyncio
async def test_lost_lease_cancels_provider_without_writeback(monkeypatch):
    from devflow import worker

    monkeypatch.setattr(worker, "HEARTBEAT_SECONDS", 0.01)
    queue = RecordingQueue(owns=False)
    cancelled = asyncio.Event()

    async def probe(job_id):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    async with asyncio.timeout(1):
        await worker._execute(queue, lease(), probe)
    assert cancelled.is_set()
    assert queue.writes == []


@pytest.mark.asyncio
async def test_attempt_deadline_cancels_provider_and_schedules_retry(monkeypatch):
    from devflow import worker

    monkeypatch.setattr(worker, "ATTEMPT_SECONDS", 0.03)
    monkeypatch.setattr(worker, "HEARTBEAT_SECONDS", 0.01)
    queue = RecordingQueue()
    cancelled = asyncio.Event()

    async def probe(job_id):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    async with asyncio.timeout(1):
        await worker._execute(queue, lease(), probe)
    assert cancelled.is_set()
    assert queue.heartbeats >= 1
    assert queue.writes == [("fail", "attempt_timeout", True, None)]


@pytest.mark.asyncio
async def test_cancellation_leaves_lease_for_recovery():
    from devflow import worker

    queue = RecordingQueue()
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def probe(job_id):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    task = asyncio.create_task(worker._execute(queue, lease(), probe))
    try:
        async with asyncio.timeout(1):
            await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert cancelled.is_set()
        assert queue.writes == []
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_provider_error_uses_safe_code_and_retry_deadline():
    from datetime import UTC, datetime

    from devflow import worker
    from devflow.errors import AppError

    queue = RecordingQueue()
    deadline = datetime(2030, 1, 1, tzinfo=UTC)

    async def probe(job_id):
        raise AppError("github_rate_limited", 503, "sensitive upstream", deadline)

    await worker._execute(queue, lease(), probe)
    assert queue.writes == [("fail", "github_rate_limited", True, deadline)]


@pytest.mark.asyncio
async def test_worker_limits_concurrency_and_stops_claiming_before_drain():
    from devflow import worker

    class ReadyQueue(RecordingQueue):
        def __init__(self):
            super().__init__()
            self.pending = [lease() for _ in range(6)]

        async def claim(self, owner):
            return self.pending.pop() if self.pending else None

        async def recover_expired(self):
            return 0

    queue = ReadyQueue()
    stop = asyncio.Event()
    release = asyncio.Event()
    four_running = asyncio.Event()
    running = set()

    async def probe(job_id):
        running.add(job_id)
        if len(running) == 4:
            four_running.set()
        try:
            await release.wait()
            return "result"
        finally:
            running.remove(job_id)

    task = asyncio.create_task(worker._serve(queue, probe, stop, concurrency=4))
    try:
        async with asyncio.timeout(1):
            await four_running.wait()
        assert len(queue.pending) == 2
        stop.set()
        release.set()
        async with asyncio.timeout(1):
            await task
        assert not running
        assert len(queue.pending) == 2
        assert queue.writes == [("complete", "result")] * 4
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_bounded_shutdown_cancels_provider_and_preserves_recovery():
    from devflow import worker

    class OneQueue(RecordingQueue):
        async def claim(self, owner):
            return lease()

        async def recover_expired(self):
            return 0

    queue = OneQueue()
    stop = asyncio.Event()
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def probe(job_id):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    task = asyncio.create_task(
        worker._serve(queue, probe, stop, concurrency=1, shutdown_seconds=0.01)
    )
    try:
        async with asyncio.timeout(1):
            await started.wait()
        stop.set()
        async with asyncio.timeout(1):
            await task
        assert cancelled.is_set()
        assert queue.writes == []
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_heartbeat_database_error_cancels_without_exposing_exception(caplog):
    import json

    from devflow import worker

    class BrokenHeartbeat(RecordingQueue):
        async def heartbeat(self, lease):
            raise RuntimeError("postgresql://password@host user-token")

    queue = BrokenHeartbeat()
    cancelled = asyncio.Event()

    async def probe(job_id):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    async with asyncio.timeout(1):
        await worker._execute(queue, lease(), probe, heartbeat_seconds=0.01)
    assert cancelled.is_set()
    assert queue.writes == []
    assert any(
        json.loads(record.message)
        == {"action": "worker.failure", "error_code": "internal_error", "count": 1}
        for record in caplog.records
        if record.name == "devflow"
    )
    assert "password" not in caplog.text
    assert "user-token" not in caplog.text
