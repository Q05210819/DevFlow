"""Independent worker process for durable repository checks."""

import asyncio
import json
import signal
import sys
import threading
import time
from uuid import uuid4

import httpx

from devflow.config import Settings
from devflow.db import create_session_factory, schema_is_current
from devflow.errors import RETRYABLE_ERROR_CODES, AppError
from devflow.github.client import GitHubClient
from devflow.github.credentials import CredentialService
from devflow.jobs.handlers import run_probe
from devflow.jobs.queue import Queue
from devflow.jobs.schemas import (
    INTERACTIVE_MODEL_JOB_KINDS,
    MODEL_JOB_KINDS,
)
from devflow.logging import configure_logging, logger
from devflow.repositories.policy import RepositoryPolicy
from devflow.security import CredentialCipher

HEARTBEAT_SECONDS = 10
ATTEMPT_SECONDS = 120


async def run_worker(
    settings: Settings,
    *,
    stop: asyncio.Event,
    github: GitHubClient | None = None,
) -> None:
    configure_logging()
    factory = create_session_factory(settings)
    http = None
    model_runtime = None
    try:
        cipher = CredentialCipher.from_file(settings.credential_encryption_key_file)
        async with asyncio.timeout(5), factory() as session:
            if not await schema_is_current(session):
                raise AppError("service_unavailable", 503, "Schema is not current")
        if github is None:
            http = httpx.AsyncClient()
            github = GitHubClient(http, settings)
        credentials = CredentialService(factory, github, cipher)
        policy = RepositoryPolicy(factory, github, credentials, settings)
        queue = Queue(
            factory,
            lease_seconds=settings.job_lease_seconds,
            retry_first_seconds=settings.job_retry_first_seconds,
            retry_second_seconds=settings.job_retry_second_seconds,
            settings=settings,
        )
        if settings.worker_role == "model":
            from devflow.knowledge.runtime import ModelRuntime

            model_runtime = ModelRuntime(settings)

        async def probe(lease):
            from sqlalchemy import select

            from devflow.github.data import GitHubDataClient
            from devflow.jobs.models import Job
            from devflow.sync.handlers import run_sync
            from devflow.sync.service import SyncService

            async with factory() as session:
                kind = await session.scalar(
                    select(Job.kind).where(Job.id == lease.job_id)
                )
            if kind == "repository.access_probe":
                return await run_probe(
                    lease.job_id, factory, policy, github, credentials
                )
            if kind == "repository.sync":
                return await run_sync(
                    lease,
                    SyncService(factory, settings),
                    GitHubDataClient(github),
                    tokens,
                )
            if kind == "repository.health":
                from devflow.health.service import HealthService

                service = HealthService(factory, policy, settings)
                frozen = await service.freeze(lease)
                await service.publish(lease, frozen)
                return None
            if kind == "repository.workspace":
                from devflow.workspace.service import WorkspaceService

                await WorkspaceService(
                    factory, policy, settings, tokens=tokens
                ).prepare(lease)
                return None
            if kind == "knowledge.cleanup":
                from devflow.knowledge.index_retention import IndexRetention
                from devflow.knowledge.milvus import MilvusIndex
                from devflow.knowledge.retention import KnowledgeRetention

                # Vectors are reclaimed first, under the same lease. The sweep
                # never finalizes the Job, so body retention still owns the
                # terminal transition and its success interval.
                await IndexRetention(factory, index=MilvusIndex(settings)).sweep(lease)
                await KnowledgeRetention(factory).sweep(lease)
                return None
            if kind == "knowledge.import":
                from devflow.knowledge.chunking import LocalChunker
                from devflow.knowledge.handlers import KnowledgeImportHandler
                from devflow.knowledge.service import KnowledgeService
                from devflow.knowledge.sources import SourceReader
                from devflow.workspace.read import WorkspaceReader

                if settings.knowledge_tokenizer_path is None:
                    raise AppError(
                        "service_unavailable", 503, "Local tokenizer is not configured"
                    )
                from devflow.workspace.handlers import owned_thread

                chunker = await owned_thread(
                    lambda: LocalChunker(settings.knowledge_tokenizer_path),
                    threading.Event(),
                )
                reader = SourceReader(
                    factory, policy, WorkspaceReader(factory, policy, retention.cache)
                )
                await KnowledgeImportHandler(
                    KnowledgeService(factory, policy, chunker=chunker, reader=reader)
                ).run(lease)
                return None
            if kind == "knowledge.index_batch":
                from devflow.knowledge.handlers import IndexBatchHandler
                from devflow.knowledge.indexing import IndexBuilder
                from devflow.knowledge.milvus import MilvusIndex
                from devflow.knowledge.service import KnowledgeService

                if model_runtime is None:
                    raise AppError("service_unavailable", 503, "Model worker required")
                await IndexBatchHandler(
                    IndexBuilder(
                        KnowledgeService(factory, policy),
                        inference=model_runtime.inference,
                        index=MilvusIndex(settings),
                    )
                ).run(lease)
                return None
            if kind == "knowledge.query":
                from devflow.knowledge.citations import CitationService
                from devflow.knowledge.handlers import QueryHandler
                from devflow.knowledge.milvus import MilvusIndex
                from devflow.knowledge.queries import QueryService
                from devflow.knowledge.retrieval import RetrievalService
                from devflow.knowledge.sources import SourceReader

                if model_runtime is None:
                    raise AppError("service_unavailable", 503, "Model worker required")
                index = MilvusIndex(settings)
                await QueryHandler(
                    QueryService(
                        factory,
                        policy,
                        retrieval=RetrievalService(
                            factory,
                            policy,
                            inference=model_runtime.inference,
                            index=index,
                        ),
                        citations=CitationService(
                            factory,
                            policy,
                            public_base_url=settings.public_base_url,
                            reader=SourceReader(factory, policy),
                        ),
                    )
                ).run(lease)
                return None
            if kind in MODEL_JOB_KINDS:
                from devflow.knowledge.handlers import MODEL_HANDLERS

                handler = MODEL_HANDLERS.get(kind)
                if handler is None:
                    raise AppError(
                        "service_unavailable", 503, "Job handler unavailable"
                    )
                await handler.run(lease)
                return None
            raise AppError("service_unavailable", 503, "Job handler unavailable")

        from devflow.github.installations import InstallationTokens
        from devflow.sync.scheduler import SyncScheduler

        tokens = InstallationTokens(github)
        scheduler = SyncScheduler(factory, settings)
        from devflow.knowledge.retention import KnowledgeMaintenance
        from devflow.workspace.git import GitCache
        from devflow.workspace.process import GitRunner
        from devflow.workspace.retention import WorkspaceRetention

        retention = WorkspaceRetention(
            factory,
            GitCache(settings.workspace_cache_root, GitRunner(settings)),
            settings,
        )

        await _serve(
            queue,
            probe,
            stop,
            concurrency=(
                1 if model_runtime is not None else settings.worker_concurrency
            ),
            heartbeat_seconds=settings.job_heartbeat_seconds,
            attempt_seconds=settings.job_attempt_timeout_seconds,
            scheduler=scheduler,
            retention=retention,
            knowledge_maintenance=KnowledgeMaintenance(factory),
            model_runtime=model_runtime,
            model_drain_timeout_seconds=settings.model_drain_timeout_seconds,
        )
    finally:
        try:
            if model_runtime is not None:
                await model_runtime.close()
        finally:
            try:
                if http is not None:
                    await http.aclose()
            finally:
                await factory.kw["bind"].dispose()


async def _serve(
    queue,
    probe,
    stop,
    *,
    concurrency,
    shutdown_seconds=30,
    heartbeat_seconds=None,
    attempt_seconds=None,
    scheduler=None,
    retention=None,
    knowledge_maintenance=None,
    model_runtime=None,
    model_drain_timeout_seconds=None,
):
    owner = uuid4()
    active = set()
    stopped = asyncio.create_task(stop.wait())
    next_maintenance = 0
    last_health = None
    try:
        while not stop.is_set():
            try:
                await queue.recover_expired()
                if scheduler is not None:
                    await scheduler.tick()
                if time.monotonic() >= next_maintenance and (
                    retention is not None or knowledge_maintenance is not None
                ):
                    next_maintenance = time.monotonic() + 60
                    if retention is not None:
                        await retention.sweep()
                    if knowledge_maintenance is not None:
                        await knowledge_maintenance.tick()
                # A model Worker never claims while its owner lock or models are
                # unavailable. Ordinary Workers keep the exact old claim call so
                # existing test doubles and admission semantics are preserved.
                model_ready = (
                    model_runtime is None or await model_runtime.ensure_ready()
                )
                if model_runtime is not None:
                    health = _runtime_health(model_runtime)
                    if health != last_health:
                        last_health = health
                        _report_model_health(*health)
                while model_ready and len(active) < concurrency and not stop.is_set():
                    if model_runtime is None:
                        lease = await queue.claim(owner)
                    else:
                        lease = await queue.claim(
                            owner,
                            allowed_kinds=MODEL_JOB_KINDS,
                            prefer_kinds=INTERACTIVE_MODEL_JOB_KINDS,
                        )
                    if lease is None or stop.is_set():
                        break
                    active.add(
                        asyncio.create_task(
                            _execute(
                                queue,
                                lease,
                                probe,
                                heartbeat_seconds=heartbeat_seconds,
                                attempt_seconds=attempt_seconds,
                                model_runtime=model_runtime,
                                model_drain_timeout_seconds=(
                                    model_drain_timeout_seconds
                                ),
                            )
                        )
                    )
            except Exception:  # noqa: BLE001
                _operational_failure()
            finished, _ = await asyncio.wait(
                active | {stopped},
                timeout=1,
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in finished - {stopped}:
                active.remove(task)
                try:
                    task.result()
                except Exception:  # noqa: BLE001
                    _operational_failure()
    finally:
        stopped.cancel()
        if active:
            _, pending = await asyncio.wait(active, timeout=shutdown_seconds)
            for task in pending:
                task.cancel()
        await asyncio.gather(stopped, *active, return_exceptions=True)


def _runtime_health(runtime) -> tuple[str, str | None]:
    report = getattr(runtime, "health", None)
    if report is None:
        # Test doubles from before the health surface only expose flags.
        return ("ready" if getattr(runtime, "ready", False) else "not_ready", None)
    health = report()
    return (health.status, health.reason)


def _report_model_health(status: str, reason: str | None) -> None:
    payload = {"action": "worker.model_runtime", "status": status, "count": 1}
    if reason is not None:
        payload["reason"] = reason
    line = json.dumps(payload)
    if status == "ready":
        logger.info(line)
    else:
        logger.warning(line)


def _operational_failure():
    logger.error(
        json.dumps(
            {"action": "worker.failure", "error_code": "internal_error", "count": 1}
        )
    )


def _consume_task(task):
    """Consume a detached task so shutdown does not report its failure."""

    if task.cancelled():
        return
    try:
        task.exception()
    except BaseException:  # noqa: BLE001, S110 - already logged/unhealthy
        pass


async def _execute(
    queue,
    lease,
    probe,
    *,
    heartbeat_seconds=None,
    attempt_seconds=None,
    model_runtime=None,
    model_drain_timeout_seconds=None,
):
    lost = False
    heartbeat_seconds = (
        HEARTBEAT_SECONDS if heartbeat_seconds is None else heartbeat_seconds
    )
    attempt_seconds = ATTEMPT_SECONDS if attempt_seconds is None else attempt_seconds
    timeout_seconds = attempt_seconds
    timeout_known = asyncio.Event()

    async def attempt():
        nonlocal timeout_seconds
        timeout_seconds = (
            await queue.attempt_timeout(lease, attempt_seconds)
            if hasattr(queue, "attempt_timeout")
            else attempt_seconds
        )
        timeout_known.set()
        async with asyncio.timeout(timeout_seconds):
            return await probe(lease)

    work = asyncio.create_task(attempt())

    async def watchdog():
        await timeout_known.wait()
        grace = (
            model_drain_timeout_seconds
            if model_drain_timeout_seconds is not None
            else 30
        )
        await asyncio.sleep(timeout_seconds + grace)
        if not work.done():
            # The timeout fired but the handler (or its physical thread) is
            # still alive. Never continue claiming another model Job.
            model_runtime.mark_unhealthy()

    guard = asyncio.create_task(watchdog()) if model_runtime is not None else None

    async def heartbeat():
        nonlocal lost
        while True:
            await asyncio.sleep(heartbeat_seconds)
            try:
                owned = await queue.heartbeat(lease)
            except Exception:  # noqa: BLE001
                # Without a confirmed renewal there is no safe writeback.
                _operational_failure()
                owned = False
            if not owned:
                lost = True
                work.cancel()
                return

    renewal = asyncio.create_task(heartbeat())
    try:
        try:
            result = await work
        except asyncio.CancelledError:
            if not lost:
                raise
        except TimeoutError:
            if not lost:
                await queue.fail(lease, "attempt_timeout", True)
        except AppError as error:
            if not lost:
                await queue.fail(
                    lease,
                    error.code,
                    error.code in RETRYABLE_ERROR_CODES,
                    error.retry_at,
                )
        except Exception:  # noqa: BLE001
            if not lost:
                await queue.fail(lease, "internal_error", False)
        else:
            if not lost and result is not None:
                await queue.complete(lease, result)
    finally:
        work.cancel()
        renewal.cancel()
        pending = [work, renewal]
        if guard is not None:
            guard.cancel()
            pending.append(guard)
        # A cancelled inference call keeps waiting for its physical thread so
        # the serialized slot is never released early. Bound that wait here,
        # otherwise a hung CUDA call would also block this Worker's heartbeat
        # and shutdown loop forever. Ownership and the slot stay held; the
        # runtime becomes terminal and the process is the only way to recover.
        grace = (
            model_drain_timeout_seconds
            if model_drain_timeout_seconds is not None
            else 30
        )
        _, stuck = await asyncio.wait(pending, timeout=max(0.1, grace))
        if stuck:
            if model_runtime is not None:
                model_runtime.mark_unhealthy("drain_timeout")
            for task in stuck:
                task.add_done_callback(_consume_task)


async def _main():
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    signals = (signal.SIGTERM, signal.SIGINT) if sys.platform != "win32" else ()
    for signum in signals:
        loop.add_signal_handler(signum, stop.set)
    try:
        await run_worker(Settings(), stop=stop)
    finally:
        for signum in signals:
            loop.remove_signal_handler(signum)


def main() -> None:
    configure_logging()
    try:
        # Psycopg requires Selector on Windows. Runner converts Ctrl+C into
        # cancellation so _serve can drain before KeyboardInterrupt reaches here.
        asyncio.run(_main(), loop_factory=asyncio.SelectorEventLoop)
    except KeyboardInterrupt:
        pass
    except Exception:  # noqa: BLE001
        _operational_failure()
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
