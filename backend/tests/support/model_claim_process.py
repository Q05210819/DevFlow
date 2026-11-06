"""Controlled model-capable Worker process for real owner/claim contention."""

import asyncio
import json
import os
import sys
import time
from pathlib import Path
from uuid import uuid4


def record(path: Path, token: str) -> None:
    with path.open("a", encoding="utf-8") as stream:
        stream.write(token + "\n")
        stream.flush()
        os.fsync(stream.fileno())


class FakeLoader:
    def __init__(self, events: Path):
        self.events = events

    def load_embedding(self, path, *, device, dtype, local_only, trust_remote_code):
        record(self.events, "load_embedding")
        return object(), 1024

    def load_reranker(self, path, *, device, dtype, local_only, trust_remote_code):
        record(self.events, "load_reranker")
        return object()

    def embed(self, model, text):
        return [1.0] * 1024

    def rerank(self, model, query, passage):
        return 0.5

    def fingerprint(self, path):
        return Path(path).name

    def synchronize(self, device):
        return None

    def close(self):
        return None


async def main() -> int:
    from devflow.config import Settings
    from devflow.db import create_session_factory
    from devflow.jobs.queue import Queue
    from devflow.jobs.schemas import (
        INTERACTIVE_MODEL_JOB_KINDS,
        MODEL_JOB_KINDS,
    )
    from devflow.knowledge.inference import LocalInference
    from devflow.knowledge.runtime import ModelRuntime

    config = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    events = Path(sys.argv[2])
    release = Path(sys.argv[3])
    seconds = float(sys.argv[4])
    settings = Settings(_env_file=None, **config)
    runtime = ModelRuntime(
        settings,
        inference=LocalInference(settings, loader=FakeLoader(events)),
    )
    factory = None
    try:
        if not await runtime.ensure_ready():
            record(events, "busy")
            return 0
        record(events, "loaded")
        factory = create_session_factory(settings)
        queue = Queue(factory, lease_seconds=30, settings=settings)
        lease = await queue.claim(
            uuid4(),
            allowed_kinds=MODEL_JOB_KINDS,
            prefer_kinds=INTERACTIVE_MODEL_JOB_KINDS,
        )
        if lease is None:
            record(events, "no-claim")
        else:
            record(events, "claimed")
            deadline = time.monotonic() + seconds
            while not release.exists() and time.monotonic() < deadline:
                await asyncio.sleep(0.02)
    finally:
        if factory is not None:
            await factory.kw["bind"].dispose()
        try:
            await runtime.close()
        except Exception:  # noqa: BLE001 - report the drain failure to the parent
            record(events, "drain-failed")
        else:
            record(events, "released")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(), loop_factory=asyncio.SelectorEventLoop))
