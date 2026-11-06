"""Controlled process used by model owner contention tests.

The process never imports a real model runtime. A recording loader only proves
the lifecycle contract: a process that loses the OS owner lock must not start
loading, and the winner loads exactly once.
"""

import asyncio
import os
import sys
from pathlib import Path

from devflow.config import Settings
from devflow.knowledge.inference import LocalInference
from devflow.knowledge.owner import ModelOwner


class RecordingLoader:
    def __init__(self, events: Path):
        self.events = events

    def _record(self, value: str) -> None:
        with self.events.open("a", encoding="utf-8") as stream:
            stream.write(value + "\n")
            stream.flush()
            os.fsync(stream.fileno())

    def load_embedding(self, path, *, device, dtype, local_only, trust_remote_code):
        self._record("load_embedding")
        return object(), 1024

    def load_reranker(self, path, *, device, dtype, local_only, trust_remote_code):
        self._record("load_reranker")
        return object()

    def embed(self, model, text):
        return [1.0] * 1024

    def rerank(self, model, query, passage):
        return 0.0

    def fingerprint(self, path):
        return Path(path).name

    def synchronize(self, device):
        return None

    def close(self):
        return None


async def hold(lock_path: Path, events: Path, release: Path, seconds: float) -> int:
    owner = ModelOwner(lock_path)
    if not owner.acquire():
        with events.open("a", encoding="utf-8") as stream:
            stream.write("busy\n")
            stream.flush()
            os.fsync(stream.fileno())
        return 0
    with events.open("a", encoding="utf-8") as stream:
        stream.write("owner\n")
        stream.flush()
        os.fsync(stream.fileno())

    root = lock_path.parent
    embedding = root / "bge-m3"
    reranker = root / "bge-reranker-v2-m3"
    embedding.mkdir(exist_ok=True)
    reranker.mkdir(exist_ok=True)
    settings = Settings(
        _env_file=None,
        app_env="test",
        public_base_url="http://localhost:3000",
        model_embedding_path=embedding,
        model_reranker_path=reranker,
        model_lock_path=lock_path,
    )
    inference = LocalInference(settings, loader=RecordingLoader(events))
    await inference.start()
    with events.open("a", encoding="utf-8") as stream:
        stream.write("loaded\n")
        stream.flush()
        os.fsync(stream.fileno())
    deadline = asyncio.get_running_loop().time() + seconds
    try:
        while asyncio.get_running_loop().time() < deadline:
            if release.exists():
                break
            await asyncio.sleep(0.02)
    finally:
        await inference.drain()
        owner.close()
        with events.open("a", encoding="utf-8") as stream:
            stream.write("released\n")
            stream.flush()
            os.fsync(stream.fileno())
    return 0


def main() -> int:
    if len(sys.argv) != 6 or sys.argv[1] != "hold":
        raise SystemExit(
            "usage: model_process.py hold <lock> <events> <release> <seconds>"
        )
    lock_path, events, release = map(Path, sys.argv[2:5])
    return asyncio.run(hold(lock_path, events, release, float(sys.argv[5])))


if __name__ == "__main__":
    raise SystemExit(main())
