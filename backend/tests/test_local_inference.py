import asyncio
import contextlib
import math
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from devflow.errors import AppError
from devflow.knowledge.inference import (
    SELF_CHECK_PASSAGE,
    SELF_CHECK_TEXT,
    LocalInference,
    ModelFingerprint,
)
from devflow.knowledge.owner import ModelOwner

BACKEND = Path(__file__).resolve().parents[1]


class FakeLoader:
    def __init__(
        self,
        *,
        dimension: int = 1024,
        vector: list[float] | None = None,
        embedding_error: Exception | None = None,
        embed_error: Exception | None = None,
        rerank_value: float = 0.5,
        block: threading.Event | None = None,
        release: threading.Event | None = None,
    ):
        self.dimension = dimension
        self.vector = vector if vector is not None else [3.0] * 1024
        self.embedding_error = embedding_error
        self.embed_error = embed_error
        self.rerank_value = rerank_value
        self.block = block
        self.release = release
        self.started = block if block is not None else threading.Event()
        self.load_embedding_calls: list[dict] = []
        self.load_reranker_calls: list[dict] = []
        self.embed_calls: list[str] = []
        self.rerank_calls: list[tuple[str, str]] = []
        self.fingerprint_calls: list[Path] = []
        self.synchronized = 0
        self.closed = 0

    def load_embedding(self, path, *, device, dtype, local_only, trust_remote_code):
        self.load_embedding_calls.append(
            {
                "path": Path(path),
                "device": device,
                "dtype": dtype,
                "local_only": local_only,
                "trust_remote_code": trust_remote_code,
            }
        )
        if self.embedding_error is not None:
            raise self.embedding_error
        return object(), self.dimension

    def load_reranker(self, path, *, device, dtype, local_only, trust_remote_code):
        self.load_reranker_calls.append(
            {
                "path": Path(path),
                "device": device,
                "dtype": dtype,
                "local_only": local_only,
                "trust_remote_code": trust_remote_code,
            }
        )
        return object()

    def embed(self, model, text):
        self.embed_calls.append(text)
        if self.block is not None:
            self.started.set()
            assert self.release is not None
            self.release.wait(30)
        if self.embed_error is not None:
            raise self.embed_error
        return list(self.vector)

    def rerank(self, model, query, passage):
        self.rerank_calls.append((query, passage))
        return self.rerank_value

    def fingerprint(self, path):
        self.fingerprint_calls.append(Path(path))
        return Path(path).name

    def synchronize(self, device):
        self.synchronized += 1

    def close(self):
        self.closed += 1


def _settings(settings_factory, root: Path, **overrides):
    embedding = root / "bge-m3"
    reranker = root / "bge-reranker-v2-m3"
    embedding.mkdir(exist_ok=True)
    reranker.mkdir(exist_ok=True)
    values = {
        "model_embedding_path": embedding,
        "model_reranker_path": reranker,
        "model_lock_path": root / "model.lock",
    }
    values.update(overrides)
    return settings_factory(**values)


def test_import_does_not_require_models_extra():
    code = (
        "import sys\n"
        "import devflow.app\n"
        "import devflow.worker\n"
        "import devflow.knowledge.inference\n"
        "assert 'torch' not in sys.modules, 'torch imported eagerly'\n"
        "assert 'sentence_transformers' not in sys.modules, "
        "'sentence_transformers imported eagerly'\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=BACKEND,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.asyncio
async def test_local_only_load_and_dimensions(settings_factory, tmp_path):
    loader = FakeLoader()
    inference = LocalInference(_settings(settings_factory, tmp_path), loader=loader)
    try:
        fingerprint = await inference.start()
        assert isinstance(fingerprint, ModelFingerprint)
        assert fingerprint == ModelFingerprint(
            embedding="bge-m3",
            reranker="bge-reranker-v2-m3",
            tokenizer="bge-m3",
            dimension=1024,
            device="cpu",
            dtype="float32",
        )
        assert inference.ready is True
        assert inference.unhealthy is False
        assert len(loader.load_embedding_calls) == 1
        assert len(loader.load_reranker_calls) == 1
        for call in (*loader.load_embedding_calls, *loader.load_reranker_calls):
            assert call["local_only"] is True
            assert call["trust_remote_code"] is False
            assert call["device"] == "cpu"
            assert call["dtype"] == "float32"

        # The bounded startup self-check runs through the real serialized slot
        # before the Worker may claim a Job.
        assert loader.embed_calls == [SELF_CHECK_TEXT]
        assert loader.rerank_calls == [(SELF_CHECK_TEXT, SELF_CHECK_PASSAGE)]
        assert loader.synchronized == 2

        vectors = await inference.embed(["alpha", "beta"])
        assert loader.embed_calls == [SELF_CHECK_TEXT, "alpha", "beta"]
        assert len(vectors) == 2
        for vector in vectors:
            assert len(vector) == 1024
            assert math.isclose(math.sqrt(sum(value**2 for value in vector)), 1.0)

        scores = await inference.rerank("question", ["one", "two"])
        assert scores == [0.5, 0.5]
        assert loader.rerank_calls == [
            (SELF_CHECK_TEXT, SELF_CHECK_PASSAGE),
            ("question", "one"),
            ("question", "two"),
        ]
        # One load phase and one self-check phase, then four inference calls.
        assert loader.synchronized == 6
    finally:
        await inference.drain()
    assert loader.closed == 0


@pytest.mark.asyncio
async def test_wrong_dimension_is_unhealthy(settings_factory, tmp_path):
    loader = FakeLoader(dimension=768)
    inference = LocalInference(_settings(settings_factory, tmp_path), loader=loader)
    with pytest.raises(AppError) as raised:
        await inference.start()
    assert raised.value.code == "service_unavailable"
    assert inference.ready is False
    assert inference.unhealthy is True
    assert inference.failure_reason == "model_load_failed"
    assert len(loader.load_embedding_calls) == 1
    await inference.drain()


@pytest.mark.asyncio
async def test_oom_is_unhealthy_and_not_retried(settings_factory, tmp_path):
    loader = FakeLoader(embedding_error=RuntimeError("CUDA out of memory"))
    inference = LocalInference(_settings(settings_factory, tmp_path), loader=loader)
    with pytest.raises(AppError) as raised:
        await inference.start()
    assert raised.value.code == "service_unavailable"
    assert inference.ready is False
    assert inference.unhealthy is True
    assert inference.failure_reason == "out_of_memory"
    assert len(loader.load_embedding_calls) == 1
    assert loader.load_reranker_calls == []
    await inference.drain()


@pytest.mark.asyncio
async def test_startup_self_check_failure_is_explicit(settings_factory, tmp_path):
    loader = FakeLoader(vector=[0.0] * 1024)
    inference = LocalInference(_settings(settings_factory, tmp_path), loader=loader)
    with pytest.raises(AppError) as raised:
        await inference.start()
    assert raised.value.code == "service_unavailable"
    assert inference.ready is False
    assert inference.unhealthy is True
    assert inference.failure_reason == "self_check_failed"
    assert loader.embed_calls == [SELF_CHECK_TEXT]
    assert loader.rerank_calls == []
    await inference.drain()


@pytest.mark.asyncio
async def test_startup_self_check_rejects_non_finite_rerank(settings_factory, tmp_path):
    loader = FakeLoader(rerank_value=float("nan"))
    inference = LocalInference(_settings(settings_factory, tmp_path), loader=loader)
    with pytest.raises(AppError):
        await inference.start()
    assert inference.failure_reason == "self_check_failed"
    assert loader.rerank_calls == [(SELF_CHECK_TEXT, SELF_CHECK_PASSAGE)]
    await inference.drain()


@pytest.mark.asyncio
async def test_startup_timeout_is_bounded_and_unhealthy(settings_factory, tmp_path):
    started = threading.Event()
    release = threading.Event()
    loader = FakeLoader(block=started, release=release)
    settings = _settings(settings_factory, tmp_path, model_startup_timeout_seconds=0.2)
    inference = LocalInference(settings, loader=loader)
    loop = asyncio.get_running_loop()
    began = loop.time()
    try:
        with pytest.raises(AppError) as raised:
            await inference.start()
        assert loop.time() - began < 5
        assert raised.value.code == "service_unavailable"
        assert inference.ready is False
        assert inference.unhealthy is True
        assert inference.failure_reason == "startup_timeout"
    finally:
        release.set()
        await inference.drain(timeout=10)


@pytest.mark.asyncio
async def test_timed_out_startup_holds_slot_and_drain_never_blocks_loop(
    settings_factory, tmp_path
):
    started = threading.Event()
    release = threading.Event()
    loader = FakeLoader(block=started, release=release)
    settings = _settings(
        settings_factory,
        tmp_path,
        model_startup_timeout_seconds=0.1,
        model_drain_timeout_seconds=1,
    )
    inference = LocalInference(settings, loader=loader)
    timer = threading.Timer(3.0, release.set)
    timer.start()
    ticks = 0

    async def ticker():
        nonlocal ticks
        while True:
            ticks += 1
            await asyncio.sleep(0.01)

    tick = asyncio.create_task(ticker())
    try:
        with pytest.raises(AppError):
            await inference.start()
        assert inference.failure_reason == "startup_timeout"

        loop = asyncio.get_running_loop()
        began = loop.time()
        with pytest.raises(AppError) as raised:
            await inference.drain()
        elapsed = loop.time() - began
        assert raised.value.code == "service_unavailable"
        assert inference.failure_reason in {"startup_timeout", "drain_timeout"}
        assert inference.unhealthy is True
        # The drain deadline must fire even though the physical startup thread
        # is still blocked; the event loop must keep making progress.
        assert elapsed < 2
        assert ticks > 5
    finally:
        tick.cancel()
        release.set()
        timer.cancel()
        await inference.drain(timeout=10)
        with contextlib.suppress(asyncio.CancelledError):
            await tick


@pytest.mark.asyncio
async def test_inference_oom_after_start_is_classified(settings_factory, tmp_path):
    loader = FakeLoader()
    inference = LocalInference(_settings(settings_factory, tmp_path), loader=loader)
    await inference.start()
    loader.embed_error = RuntimeError("CUDA out of memory")
    with pytest.raises(AppError) as raised:
        await inference.embed(["after-start"])
    assert raised.value.code == "service_unavailable"
    assert inference.ready is False
    assert inference.unhealthy is True
    assert inference.failure_reason == "out_of_memory"
    await inference.drain()


@pytest.mark.asyncio
async def test_cancel_waits_for_physical_inference(settings_factory, tmp_path):
    started = threading.Event()
    release = threading.Event()
    loader = FakeLoader()
    inference = LocalInference(_settings(settings_factory, tmp_path), loader=loader)
    await inference.start()
    loader.block = started
    loader.release = release
    loader.started = started
    try:
        first = asyncio.create_task(inference.embed(["first"]))
        assert await asyncio.to_thread(started.wait, 10)
        first.cancel()
        second = asyncio.create_task(inference.embed(["second"]))
        await asyncio.sleep(0.1)
        assert loader.embed_calls == [SELF_CHECK_TEXT, "first"]
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await first
        vectors = await second
        assert len(vectors) == 1
        assert len(vectors[0]) == 1024
        assert loader.embed_calls == [SELF_CHECK_TEXT, "first", "second"]
    finally:
        release.set()
        await inference.drain()


@pytest.mark.asyncio
async def test_hung_inference_stops_claim(settings_factory, tmp_path):
    started = threading.Event()
    release = threading.Event()
    loader = FakeLoader()
    settings = _settings(settings_factory, tmp_path)
    owner = ModelOwner(settings.model_lock_path)
    assert owner.acquire() is True
    inference = LocalInference(settings, loader=loader)
    await inference.start()
    loader.block = started
    loader.release = release
    loader.started = started
    task = asyncio.create_task(inference.embed(["first"]))
    assert await asyncio.to_thread(started.wait, 10)
    try:
        with pytest.raises(AppError) as raised:
            await inference.drain(timeout=0.2)
        assert raised.value.code == "service_unavailable"
        assert inference.ready is False
        assert inference.unhealthy is True
        assert owner.acquired is True
        assert ModelOwner(settings.model_lock_path).acquire() is False
        with pytest.raises(AppError):
            await inference.embed(["second"])
        assert loader.embed_calls == [SELF_CHECK_TEXT, "first"]
    finally:
        release.set()
        task.cancel()
        with contextlib.suppress(BaseException):
            await task
        await inference.drain(timeout=10)
        owner.close()
    assert owner.acquired is False
