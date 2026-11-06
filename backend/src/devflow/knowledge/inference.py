"""One serialized local inference slot owned by the model-capable Worker.

Importing this module never imports PyTorch or Sentence Transformers. The real
runtime is imported only inside ``LocalInference.start`` after the owner lock is
held, so an API or ordinary Worker without the ``models`` extra still starts.
"""

import asyncio
import hashlib
import math
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from devflow.config import Settings
from devflow.errors import AppError

DIMENSION = 1024

SELF_CHECK_TEXT = "devflow local model self-check"
SELF_CHECK_PASSAGE = "devflow local model self-check passage"

FailureReason = Literal[
    "model_path_missing",
    "model_load_failed",
    "out_of_memory",
    "startup_cancelled",
    "startup_timeout",
    "self_check_failed",
    "inference_failed",
    "drain_timeout",
]


def _failure_reason_for(error: BaseException, default: FailureReason) -> FailureReason:
    if isinstance(error, MemoryError):
        return "out_of_memory"
    message = str(error).lower()
    if "out of memory" in message or "cuda oom" in message or "alloc failed" in message:
        return "out_of_memory"
    return default


def _consume_result(work: asyncio.Future) -> None:
    """Consume a detached startup result so it cannot warn at shutdown."""

    if work.cancelled():
        return
    try:
        work.exception()
    except BaseException:  # noqa: BLE001, S110 - detached failure is already recorded
        pass


@dataclass(frozen=True, slots=True)
class ModelFingerprint:
    embedding: str
    reranker: str
    tokenizer: str
    dimension: int
    device: str
    dtype: str


class ModelLoader(Protocol):
    def load_embedding(
        self,
        path: Path,
        *,
        device: str,
        dtype: str,
        local_only: bool,
        trust_remote_code: bool,
    ) -> tuple[object, int]: ...

    def load_reranker(
        self,
        path: Path,
        *,
        device: str,
        dtype: str,
        local_only: bool,
        trust_remote_code: bool,
    ) -> object: ...

    def embed(self, model: object, text: str) -> Sequence[float]: ...

    def rerank(self, model: object, query: str, passage: str) -> float: ...

    def embedding_tokens(self, model: object, text: str) -> int: ...

    def rerank_tokens(self, model: object, query: str, passage: str) -> int: ...

    def fingerprint(self, path: Path) -> str: ...

    def synchronize(self, device: str) -> None: ...

    def close(self) -> None: ...


def _directory_fingerprint(path: Path) -> str:
    digest = hashlib.sha256()
    if path.is_file():
        files = [path]
        root = path.parent
    else:
        files = sorted(item for item in path.rglob("*") if item.is_file())
        root = path
    for item in files:
        stat = item.stat()
        name = item.relative_to(root).as_posix()
        digest.update(f"{name}:{stat.st_size}:{stat.st_mtime_ns}\n".encode())
    return f"{path.name}:{digest.hexdigest()[:16]}"


class _SentenceTransformerLoader:
    """Lazy local-only Sentence Transformers adapter for bge-m3 models."""

    def load_embedding(
        self,
        path: Path,
        *,
        device: str,
        dtype: str,
        local_only: bool,
        trust_remote_code: bool,
    ) -> tuple[object, int]:
        import torch
        from sentence_transformers import SentenceTransformer

        model = SentenceTransformer(
            str(path),
            device=device,
            local_files_only=local_only,
            trust_remote_code=trust_remote_code,
            model_kwargs={"torch_dtype": getattr(torch, dtype)},
        )
        return model, int(model.get_sentence_embedding_dimension())

    def load_reranker(
        self,
        path: Path,
        *,
        device: str,
        dtype: str,
        local_only: bool,
        trust_remote_code: bool,
    ) -> object:
        import torch
        from sentence_transformers import CrossEncoder

        return CrossEncoder(
            str(path),
            device=device,
            local_files_only=local_only,
            trust_remote_code=trust_remote_code,
            automodel_args={"torch_dtype": getattr(torch, dtype)},
        )

    def embed(self, model: object, text: str) -> Sequence[float]:
        return model.encode(
            text,
            batch_size=1,
            normalize_embeddings=True,
            convert_to_numpy=True,
            show_progress_bar=False,
        ).tolist()

    def rerank(self, model: object, query: str, passage: str) -> float:
        return float(
            model.predict([(query, passage)], batch_size=1, show_progress_bar=False)[0]
        )

    def embedding_tokens(self, model: object, text: str) -> int:
        # Count the real tokenizer output including special tokens; never
        # truncate, because a silent cut would invalidate the 3A chunk bounds.
        return len(
            model.tokenizer(text, add_special_tokens=True, truncation=False)[
                "input_ids"
            ]
        )

    def rerank_tokens(self, model: object, query: str, passage: str) -> int:
        return len(
            model.tokenizer(
                query,
                passage,
                add_special_tokens=True,
                truncation=False,
                verbose=False,
            )["input_ids"]
        )

    def fingerprint(self, path: Path) -> str:
        return _directory_fingerprint(Path(path))

    def synchronize(self, device: str) -> None:
        if device == "cuda":
            import torch

            torch.cuda.synchronize()

    def close(self) -> None:
        return None


def _normalize(values: Sequence[float]) -> list[float]:
    vector = [float(value) for value in values]
    if len(vector) != DIMENSION:
        raise ValueError("Embedding dimension must be 1024")
    norm = math.sqrt(sum(value * value for value in vector))
    if not math.isfinite(norm) or norm == 0:
        raise ValueError("Embedding vector must be finite and non-zero")
    return [value / norm for value in vector]


class LocalInference:
    def __init__(self, settings: Settings, *, loader: ModelLoader | None = None):
        self._settings = settings
        self._loader = loader
        self._executor: ThreadPoolExecutor | None = None
        self._slot = asyncio.Lock()
        self._started = False
        self._unhealthy = False
        self._closing = False
        self._drained = False
        self._fingerprint: ModelFingerprint | None = None
        self._failure_reason: FailureReason | None = None
        self._embedding_model: object | None = None
        self._reranker_model: object | None = None

    @property
    def ready(self) -> bool:
        return self._started and not self._unhealthy and not self._closing

    @property
    def unhealthy(self) -> bool:
        return self._unhealthy

    @property
    def fingerprint(self) -> ModelFingerprint | None:
        return self._fingerprint

    @property
    def failure_reason(self) -> FailureReason | None:
        return self._failure_reason

    def mark_unhealthy(self, reason: FailureReason | None = None) -> None:
        """Stop accepting work; a possibly live physical thread keeps the slot."""

        self._unhealthy = True
        self._started = False
        if reason is not None:
            self._failure_reason = reason

    def _required_path(self, name: str) -> Path:
        value = getattr(self._settings, name)
        if value is None:
            self._failure_reason = "model_path_missing"
            raise AppError(
                "service_unavailable", 503, f"Local model path {name} is not configured"
            )
        path = Path(value)
        if not path.is_dir():
            self._failure_reason = "model_path_missing"
            raise AppError(
                "service_unavailable", 503, f"Local model directory {name} is missing"
            )
        return path

    def _load_models(self) -> ModelFingerprint:
        loader = self._loader
        if loader is None:
            loader = self._loader = _SentenceTransformerLoader()
        embedding = self._required_path("model_embedding_path")
        reranker = self._required_path("model_reranker_path")
        tokenizer = self._settings.knowledge_tokenizer_path or embedding
        tokenizer = Path(tokenizer)
        if not tokenizer.is_dir():
            raise ValueError("Local tokenizer directory is missing")
        model, dimension = loader.load_embedding(
            embedding,
            device=self._settings.model_device,
            dtype=self._settings.model_dtype,
            local_only=True,
            trust_remote_code=False,
        )
        if dimension != DIMENSION:
            raise ValueError("Embedding dimension must be 1024")
        reranker_model = loader.load_reranker(
            reranker,
            device=self._settings.model_device,
            dtype=self._settings.model_dtype,
            local_only=True,
            trust_remote_code=False,
        )
        self._embedding_model = model
        self._reranker_model = reranker_model
        return ModelFingerprint(
            embedding=loader.fingerprint(embedding),
            reranker=loader.fingerprint(reranker),
            tokenizer=loader.fingerprint(tokenizer),
            dimension=DIMENSION,
            device=self._settings.model_device,
            dtype=self._settings.model_dtype,
        )

    def _self_check(self) -> None:
        """Prove the loaded models can run before the Worker claims Jobs."""

        loader = self._loader
        assert loader is not None
        normalized = _normalize(loader.embed(self._embedding_model, SELF_CHECK_TEXT))
        if not all(math.isfinite(value) for value in normalized):
            raise ValueError("Embedding self-check produced a non-finite vector")
        score = loader.rerank(self._reranker_model, SELF_CHECK_TEXT, SELF_CHECK_PASSAGE)
        if not math.isfinite(float(score)):
            raise ValueError("Reranker self-check produced a non-finite score")

    async def _bounded(self, function, deadline: float, reason: FailureReason):
        """Run one startup phase within the remaining startup deadline.

        The physical thread cannot be interrupted. On timeout it keeps the
        executor slot and the owner lock, while this coroutine reports an
        explicit unhealthy state so no Job is claimed.
        """

        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            self._unhealthy = True
            self._failure_reason = "startup_timeout"
            raise AppError("service_unavailable", 503, "Local model startup timed out")
        work = asyncio.ensure_future(self._run_startup_physical(function))
        try:
            done, _ = await asyncio.wait({work}, timeout=remaining)
        except asyncio.CancelledError:
            # Cancelling before the physical call begins must not start a
            # model load; once it has begun, ``_run_physical`` keeps waiting
            # for the real thread and releases the slot only after it returns.
            work.cancel()
            work.add_done_callback(_consume_result)
            raise
        if not done:
            work.add_done_callback(_consume_result)
            self._unhealthy = True
            self._failure_reason = "startup_timeout"
            raise AppError("service_unavailable", 503, "Local model startup timed out")
        try:
            return work.result()
        except BaseException as error:
            if self._failure_reason is None:
                self._failure_reason = _failure_reason_for(error, reason)
            raise

    def _physical(self, function):
        result = function()
        if self._loader is not None:
            self._loader.synchronize(self._settings.model_device)
        return result

    async def _run_startup_physical(self, function):
        """Hold the real inference slot for every startup phase.

        A timed-out startup task keeps this slot until its physical thread
        returns, so ``drain`` cannot mistake a still-loading model for idle
        work and block the event loop in ``shutdown(wait=True)``.
        """

        async with self._slot:
            return await self._run_physical(function)

    async def _run_physical(self, function):
        assert self._executor is not None
        work = asyncio.wrap_future(self._executor.submit(self._physical, function))
        try:
            return await asyncio.shield(work)
        except asyncio.CancelledError:
            # A cancelled coroutine must not release the slot while CUDA may
            # still be writing. Repeated cancellation keeps waiting.
            while not work.done():
                try:
                    await asyncio.shield(work)
                except asyncio.CancelledError:
                    continue
                except BaseException:  # noqa: BLE001 - cancellation is preserved
                    break
            if work.done() and not work.cancelled():
                try:
                    work.exception()
                except BaseException:  # noqa: BLE001, S110
                    # The caller's cancellation is the contract; a physical
                    # failure during drain is intentionally not re-raised here.
                    pass
            raise

    def _require_ready(self) -> None:
        if not self.ready:
            raise AppError("service_unavailable", 503, "Local inference is not ready")

    async def start(self) -> ModelFingerprint:
        if self._started and self._fingerprint is not None:
            return self._fingerprint
        if self._unhealthy:
            raise AppError(
                "service_unavailable",
                503,
                f"Local inference is unhealthy ({self._failure_reason})",
            )
        if self._executor is None:
            self._executor = ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="devflow-inference"
            )
        deadline = (
            asyncio.get_running_loop().time()
            + self._settings.model_startup_timeout_seconds
        )
        try:
            fingerprint = await self._bounded(
                self._load_models, deadline, "model_load_failed"
            )
            await self._bounded(self._self_check, deadline, "self_check_failed")
        except asyncio.CancelledError:
            self._unhealthy = True
            if self._failure_reason is None:
                self._failure_reason = "startup_cancelled"
            raise
        except AppError:
            self._unhealthy = True
            raise
        except Exception as error:
            self._unhealthy = True
            if self._failure_reason is None:
                self._failure_reason = _failure_reason_for(error, "model_load_failed")
            raise AppError(
                "service_unavailable", 503, "Local model initialization failed"
            ) from error
        self._fingerprint = fingerprint
        self._started = True
        self._failure_reason = None
        return fingerprint

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        self._require_ready()
        if not texts:
            return []
        vectors = []
        try:
            for text in texts:
                vector = await self._call(
                    lambda text=text: self._loader.embed(self._embedding_model, text)
                )
                vectors.append(_normalize(vector))
        except AppError:
            raise
        except Exception as error:
            self._unhealthy = True
            self._failure_reason = _failure_reason_for(error, "inference_failed")
            raise AppError(
                "service_unavailable", 503, "Local inference failed"
            ) from error
        return vectors

    async def rerank(self, query: str, passages: Sequence[str]) -> list[float]:
        self._require_ready()
        if not passages:
            return []
        scores = []
        try:
            for passage in passages:
                score = await self._call(
                    lambda passage=passage: self._loader.rerank(
                        self._reranker_model, query, passage
                    )
                )
                value = float(score)
                if not math.isfinite(value):
                    raise ValueError("Rerank score must be finite")
                scores.append(value)
        except AppError:
            raise
        except Exception as error:
            self._unhealthy = True
            self._failure_reason = _failure_reason_for(error, "inference_failed")
            raise AppError(
                "service_unavailable", 503, "Local inference failed"
            ) from error
        return scores

    async def _count(self, function) -> int:
        self._require_ready()
        try:
            count = int(await self._call(function))
        except AppError:
            raise
        except Exception as error:
            self._unhealthy = True
            self._failure_reason = _failure_reason_for(error, "inference_failed")
            raise AppError(
                "service_unavailable", 503, "Local inference failed"
            ) from error
        if count < 0:
            raise AppError("service_unavailable", 503, "Local inference failed")
        return count

    async def count_embedding_tokens(self, text: str) -> int:
        return await self._count(
            lambda: self._loader.embedding_tokens(self._embedding_model, text)
        )

    async def count_rerank_tokens(self, query: str, passage: str) -> int:
        return await self._count(
            lambda: self._loader.rerank_tokens(self._reranker_model, query, passage)
        )

    async def _call(self, function):
        async with self._slot:
            self._require_ready()
            return await self._run_physical(function)

    async def drain(self, *, timeout: float | None = None) -> None:
        self._closing = True
        if self._drained or self._executor is None:
            return
        wait = (
            self._settings.model_drain_timeout_seconds if timeout is None else timeout
        )
        try:
            await asyncio.wait_for(self._slot.acquire(), wait)
        except TimeoutError:
            self._unhealthy = True
            if self._failure_reason is None:
                self._failure_reason = "drain_timeout"
            raise AppError(
                "service_unavailable", 503, "Local inference did not drain"
            ) from None
        try:
            # The slot is only acquired when no physical call is active. Do
            # the join off-loop anyway: a join must never freeze the worker's
            # heartbeats or stop handling if an execution path is unexpected.
            await asyncio.to_thread(self._executor.shutdown, True)
            self._drained = True
            self._started = False
        finally:
            self._slot.release()
