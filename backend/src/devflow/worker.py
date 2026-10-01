"""Independent worker process for durable repository checks."""

import asyncio
import json
import signal
import sys
from uuid import uuid4

import httpx

from devflow.config import Settings
from devflow.db import create_session_factory, schema_is_current
from devflow.errors import RETRYABLE_ERROR_CODES, AppError
from devflow.github.client import GitHubClient
from devflow.github.credentials import CredentialService
from devflow.jobs.handlers import run_probe
from devflow.jobs.queue import Queue
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
        )

        async def probe(job_id):
            return await run_probe(job_id, factory, policy, github, credentials)

        await _serve(
            queue,
            probe,
            stop,
            concurrency=settings.worker_concurrency,
            heartbeat_seconds=settings.job_heartbeat_seconds,
            attempt_seconds=settings.job_attempt_timeout_seconds,
        )
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
):
    owner = uuid4()
    active = set()
    stopped = asyncio.create_task(stop.wait())
    try:
        while not stop.is_set():
            try:
                await queue.recover_expired()
                while len(active) < concurrency and not stop.is_set():
                    lease = await queue.claim(owner)
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


def _operational_failure():
    logger.error(
        json.dumps(
            {"action": "worker.failure", "error_code": "internal_error", "count": 1}
        )
    )


async def _execute(
    queue, lease, probe, *, heartbeat_seconds=None, attempt_seconds=None
):
    lost = False
    heartbeat_seconds = (
        HEARTBEAT_SECONDS if heartbeat_seconds is None else heartbeat_seconds
    )
    attempt_seconds = ATTEMPT_SECONDS if attempt_seconds is None else attempt_seconds

    async def attempt():
        async with asyncio.timeout(attempt_seconds):
            return await probe(lease.job_id)

    work = asyncio.create_task(attempt())

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
            if not lost:
                await queue.complete(lease, result)
    finally:
        work.cancel()
        renewal.cancel()
        await asyncio.gather(work, renewal, return_exceptions=True)


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
