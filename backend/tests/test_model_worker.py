"""Model Worker serial execution, independent heartbeat and unhealthy stop."""

import asyncio
from uuid import uuid4

import pytest

from devflow.jobs.queue import Lease


class FakeInference:
    def __init__(self):
        self.ready = True
        self.unhealthy = False
        self.calls: list[tuple[str, ...]] = []
        self.drains = 0

    async def embed(self, texts):
        self.calls.append(tuple(texts))
        return [[0.0] * 1024 for _ in texts]

    async def rerank(self, query, passages):
        return [0.0 for _ in passages]

    def mark_unhealthy(self):
        self.unhealthy = True
        self.ready = False

    async def drain(self, *, timeout=None):
        self.drains += 1
        self.ready = False


class BlockingInference(FakeInference):
    def __init__(self):
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def embed(self, texts):
        self.calls.append(tuple(texts))
        self.started.set()
        await self.release.wait()
        return [[0.0] * 1024 for _ in texts]


class FakeRuntime:
    def __init__(self, *, inference=None, ready=True):
        self.inference = inference or FakeInference()
        self.ready = ready
        self.unhealthy = False
        self.ensure_calls = 0
        self.closes = 0

    async def ensure_ready(self):
        self.ensure_calls += 1
        return self.ready

    def mark_unhealthy(self):
        self.unhealthy = True
        self.ready = False
        self.inference.mark_unhealthy()

    async def close(self):
        self.closes += 1
        await self.inference.drain()


class ModelQueue:
    def __init__(self, leases, *, owns=True, timeout=5):
        self.pending = list(leases)
        self.claimed = []
        self.claim_kwargs = []
        self.heartbeats = 0
        self.writes = []
        self.owns = owns
        self.timeout = timeout

    async def recover_expired(self):
        return 0

    async def claim(self, owner, *, allowed_kinds=None, prefer_kinds=()):
        self.claim_kwargs.append((allowed_kinds, prefer_kinds))
        if not self.pending:
            return None
        lease = self.pending.pop(0)
        self.claimed.append(lease)
        return lease

    async def attempt_timeout(self, lease, default):
        return self.timeout

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
    return Lease(uuid4(), uuid4(), 1, 1)


@pytest.mark.asyncio
async def test_chat_occupies_worker_during_http():
    from devflow import worker
    from devflow.jobs.schemas import INTERACTIVE_MODEL_JOB_KINDS, MODEL_JOB_KINDS

    chat = lease()
    query = lease()
    queue = ModelQueue([chat, query])
    runtime = FakeRuntime()
    started = asyncio.Event()
    release = asyncio.Event()
    query_started = asyncio.Event()

    async def probe(received):
        if received is chat:
            started.set()
            await release.wait()
        else:
            query_started.set()

    stop = asyncio.Event()
    task = asyncio.create_task(
        worker._serve(
            queue,
            probe,
            stop,
            concurrency=1,
            heartbeat_seconds=0.01,
            attempt_seconds=5,
            model_runtime=runtime,
            model_drain_timeout_seconds=0.05,
        )
    )
    try:
        async with asyncio.timeout(2):
            await started.wait()
        await asyncio.sleep(0.1)
        assert queue.claimed == [chat]
        assert queue.heartbeats >= 1
        assert not query_started.is_set()
        assert runtime.inference.calls == []
        assert queue.claim_kwargs[0] == (
            MODEL_JOB_KINDS,
            INTERACTIVE_MODEL_JOB_KINDS,
        )

        release.set()
        async with asyncio.timeout(3):
            await query_started.wait()
        assert queue.claimed == [chat, query]
    finally:
        stop.set()
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_direct_retrieval_uses_parent_lease_and_same_serial_slot():
    from devflow import worker

    first = lease()
    second = lease()
    queue = ModelQueue([first, second])
    inference = BlockingInference()
    runtime = FakeRuntime(inference=inference)
    seen = []
    first_started = asyncio.Event()
    second_done = asyncio.Event()

    async def probe(received):
        seen.append(received)
        if received is first:
            first_started.set()
        await runtime.inference.embed(["question"])
        if received is second:
            second_done.set()

    stop = asyncio.Event()
    task = asyncio.create_task(
        worker._serve(
            queue,
            probe,
            stop,
            concurrency=1,
            heartbeat_seconds=0.01,
            attempt_seconds=5,
            model_runtime=runtime,
        )
    )
    try:
        async with asyncio.timeout(2):
            await first_started.wait()
        await asyncio.sleep(0.05)
        assert queue.claimed == [first]
        assert seen == [first]
        inference.release.set()
        async with asyncio.timeout(4):
            await second_done.wait()
        assert queue.claimed == [first, second]
        assert seen == [first, second]
        assert inference.calls == [("question",), ("question",)]
        # No child model Job was enqueued or claimed for either retrieval.
        assert len(queue.claimed) == 2
    finally:
        stop.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_unhealthy_runtime_does_not_claim():
    from devflow import worker

    queue = ModelQueue([lease()])
    runtime = FakeRuntime(ready=False)
    stop = asyncio.Event()
    task = asyncio.create_task(
        worker._serve(
            queue,
            lambda received: None,
            stop,
            concurrency=1,
            model_runtime=runtime,
        )
    )
    try:
        await asyncio.sleep(0.1)
        assert queue.claimed == []
        assert runtime.ensure_calls >= 1
    finally:
        stop.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_hung_handler_marks_runtime_unhealthy():
    from devflow import worker

    queue = ModelQueue([], timeout=0.05)
    runtime = FakeRuntime()
    started = asyncio.Event()
    release = asyncio.Event()

    async def probe(received):
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await release.wait()
            raise

    task = asyncio.create_task(
        worker._execute(
            queue,
            lease(),
            probe,
            heartbeat_seconds=10,
            attempt_seconds=0.05,
            model_runtime=runtime,
            model_drain_timeout_seconds=0.05,
        )
    )
    try:
        async with asyncio.timeout(2):
            await started.wait()
        await asyncio.sleep(0.2)
        assert runtime.unhealthy is True
        assert runtime.ready is False
        release.set()
        async with asyncio.timeout(2):
            await task
        assert queue.writes == [("fail", "attempt_timeout", True, None)]
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


class RecordingOwner:
    def __init__(self, events=None, *, can_acquire=True):
        self.can_acquire = can_acquire
        self.acquired = False
        self.events = [] if events is None else events

    def acquire(self):
        self.events.append("acquire")
        self.acquired = self.can_acquire
        return self.acquired

    def close(self):
        self.events.append("close")
        self.acquired = False


class RecordingInference(FakeInference):
    def __init__(self, events, *, fail_start=False, fail_drain=False):
        super().__init__()
        self.events = events
        self.fail_start = fail_start
        self.fail_drain = fail_drain
        self.ready = False

    async def start(self):
        self.events.append("start")
        if self.fail_start:
            raise RuntimeError("model initialization failed")
        self.ready = True

    async def drain(self, *, timeout=None):
        self.events.append("drain")
        if self.fail_drain:
            self.unhealthy = True
            from devflow.errors import AppError

            raise AppError("service_unavailable", 503, "did not drain")
        self.ready = False


@pytest.mark.asyncio
async def test_runtime_acquires_owner_before_start_and_releases_after_drain(
    settings_factory,
):
    from devflow.knowledge.runtime import ModelRuntime

    events = []
    owner = RecordingOwner(events)
    inference = RecordingInference(events)
    runtime = ModelRuntime(settings_factory(), owner=owner, inference=inference)

    assert await runtime.ensure_ready() is True
    assert events == ["acquire", "start"]
    assert runtime.ready is True

    await runtime.close()
    assert events == ["acquire", "start", "drain", "close"]
    assert owner.acquired is False


@pytest.mark.asyncio
async def test_runtime_does_not_start_without_owner(settings_factory):
    from devflow.knowledge.runtime import ModelRuntime

    events = []
    owner = RecordingOwner(events, can_acquire=False)
    inference = RecordingInference(events)
    runtime = ModelRuntime(settings_factory(), owner=owner, inference=inference)

    assert await runtime.ensure_ready() is False
    assert events == ["acquire"]
    await runtime.close()
    assert events == ["acquire", "close"]


@pytest.mark.asyncio
async def test_runtime_mark_unhealthy_is_terminal(settings_factory):
    from devflow.knowledge.runtime import ModelRuntime

    events = []
    owner = RecordingOwner(events)
    inference = RecordingInference(events)
    runtime = ModelRuntime(settings_factory(), owner=owner, inference=inference)
    assert await runtime.ensure_ready() is True

    runtime.mark_unhealthy()
    assert runtime.ready is False
    assert runtime.unhealthy is True
    assert inference.unhealthy is True
    assert await runtime.ensure_ready() is False
    assert events.count("start") == 1

    await runtime.close()
    assert owner.acquired is True
    assert "close" not in events


@pytest.mark.asyncio
async def test_runtime_health_reports_owner_busy(settings_factory):
    from devflow.knowledge.runtime import ModelRuntime

    events = []
    owner = RecordingOwner(events, can_acquire=False)
    inference = RecordingInference(events)
    runtime = ModelRuntime(settings_factory(), owner=owner, inference=inference)

    assert runtime.health().status == "not_ready"
    assert runtime.health().reason == "not_started"
    assert await runtime.ensure_ready() is False
    assert runtime.health().status == "not_ready"
    assert runtime.health().reason == "owner_locked"
    await runtime.close()


@pytest.mark.asyncio
async def test_runtime_health_transitions_ready_and_unhealthy(settings_factory):
    from devflow.knowledge.runtime import ModelRuntime

    events = []
    owner = RecordingOwner(events)
    inference = RecordingInference(events)
    runtime = ModelRuntime(settings_factory(), owner=owner, inference=inference)

    assert await runtime.ensure_ready() is True
    assert runtime.health().status == "ready"
    assert runtime.health().fingerprint is None

    runtime.mark_unhealthy("out_of_memory")
    health = runtime.health()
    assert health.status == "unhealthy"
    assert health.reason == "out_of_memory"
    assert runtime.ready is False


@pytest.mark.asyncio
async def test_runtime_health_reports_start_failure(settings_factory):
    from devflow.knowledge.runtime import ModelRuntime

    events = []
    owner = RecordingOwner(events)
    inference = RecordingInference(events, fail_start=True)
    runtime = ModelRuntime(settings_factory(), owner=owner, inference=inference)

    assert await runtime.ensure_ready() is False
    health = runtime.health()
    assert health.status == "unhealthy"
    assert health.reason == "model_load_failed"
    assert owner.acquired is False


@pytest.mark.asyncio
async def test_runtime_keeps_owner_when_drain_cannot_finish(settings_factory):
    from devflow.errors import AppError
    from devflow.knowledge.runtime import ModelRuntime

    events = []
    owner = RecordingOwner(events)
    inference = RecordingInference(events, fail_drain=True)
    runtime = ModelRuntime(settings_factory(), owner=owner, inference=inference)
    assert await runtime.ensure_ready() is True

    with pytest.raises(AppError):
        await runtime.close()
    assert owner.acquired is True
    assert "close" not in events
