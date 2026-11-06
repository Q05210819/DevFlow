"""Real local model runtime acceptance: ownership, latency, memory, cancellation.

Every case here runs the real ``sentence-transformers``/``torch`` runtime with
the operator-selected local model directories. Without
``TEST_MODEL_EMBEDDING_PATH``, ``TEST_MODEL_RERANKER_PATH``,
``TEST_MODEL_DEVICE`` and ``TEST_MODEL_DTYPE`` the whole module is reported
NOT EXECUTED; a fake loader is never substituted under the same label.

Owner suspension and termination are exercised on the platform under test.
Windows and Linux results are never inferred from one another.
"""

import asyncio
import ctypes
import json
import math
import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import asdict
from pathlib import Path

import pytest

from devflow.config import Settings
from devflow.knowledge.inference import LocalInference, _SentenceTransformerLoader
from devflow.knowledge.owner import ModelOwner
from devflow.knowledge.runtime import ModelRuntime

BACKEND = Path(__file__).resolve().parents[2]
OWNER_SCRIPT = BACKEND / "tests" / "support" / "model_process.py"

QUERY = "How does the worker publish a knowledge index?"
PASSAGE = "The worker writes vectors before publishing the active index version."

RUNTIME_ENV = {
    "TEST_MODEL_EMBEDDING_PATH": os.environ.get("TEST_MODEL_EMBEDDING_PATH"),
    "TEST_MODEL_RERANKER_PATH": os.environ.get("TEST_MODEL_RERANKER_PATH"),
    "TEST_MODEL_DEVICE": os.environ.get("TEST_MODEL_DEVICE"),
    "TEST_MODEL_DTYPE": os.environ.get("TEST_MODEL_DTYPE"),
}
MISSING = [name for name, value in RUNTIME_ENV.items() if not value]

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        bool(MISSING),
        reason=("real local model runtime NOT EXECUTED; missing " + ", ".join(MISSING)),
    ),
]


def runtime_settings(root: Path) -> Settings:
    values = {
        name: RUNTIME_ENV[name]
        for name in (
            "TEST_MODEL_EMBEDDING_PATH",
            "TEST_MODEL_RERANKER_PATH",
        )
    }
    for name, raw in values.items():
        path = Path(raw)
        assert path.is_absolute(), f"{name} must be an absolute directory"
        assert path.is_dir(), f"{name} does not exist: {path}"
    timeout = os.environ.get("TEST_MODEL_STARTUP_TIMEOUT_SECONDS", "1800")
    return Settings(
        _env_file=None,
        app_env="test",
        public_base_url="http://localhost:3000",
        worker_role="model",
        model_embedding_path=values["TEST_MODEL_EMBEDDING_PATH"],
        model_reranker_path=values["TEST_MODEL_RERANKER_PATH"],
        model_device=RUNTIME_ENV["TEST_MODEL_DEVICE"],
        model_dtype=RUNTIME_ENV["TEST_MODEL_DTYPE"],
        model_lock_path=root / "model-owner.lock",
        model_startup_timeout_seconds=float(timeout),
        model_drain_timeout_seconds=120,
        knowledge_tokenizer_path=os.environ.get("TEST_MODEL_TOKENIZER_PATH"),
    )


def peak_rss_bytes() -> int:
    if os.name == "nt":
        from ctypes import wintypes

        class ProcessMemoryCounters(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        counters = ProcessMemoryCounters()
        counters.cb = ctypes.sizeof(counters)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        psapi.GetProcessMemoryInfo.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(ProcessMemoryCounters),
            wintypes.DWORD,
        ]
        psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
        if not psapi.GetProcessMemoryInfo(
            kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb
        ):
            raise OSError(ctypes.get_last_error(), "GetProcessMemoryInfo failed")
        return int(counters.PeakWorkingSetSize)

    import resource

    # ru_maxrss is KiB on Linux.
    return int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss) * 1024


def gpu_report(device: str) -> dict:
    report = {
        "accelerator": device,
        "cuda_available": False,
        "gpu_name": None,
        "peak_vram_bytes": None,
        "coexisting_gpu_services": None,
    }
    if device != "cuda":
        return report

    import torch

    available = torch.cuda.is_available()
    report["cuda_available"] = available
    if available:
        report["gpu_name"] = torch.cuda.get_device_name(0)
        report["peak_vram_bytes"] = int(torch.cuda.max_memory_allocated(0))
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-compute-apps=pid,process_name,used_gpu_memory",
                "--format=csv,noheader",
            ],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        if result.returncode == 0:
            report["coexisting_gpu_services"] = [
                line.strip() for line in result.stdout.splitlines() if line.strip()
            ]
    except (OSError, subprocess.SubprocessError):
        report["coexisting_gpu_services"] = None
    return report


def norm(vector: list[float]) -> float:
    return math.sqrt(sum(value * value for value in vector))


async def test_runtime_owner_and_memory_report(workspace_tmp_path, record_property):
    settings = runtime_settings(workspace_tmp_path)
    runtime = ModelRuntime(settings)
    try:
        assert runtime.health().status == "not_ready"
        assert runtime.health().reason == "not_started"

        if settings.model_device == "cuda":
            import torch

            torch.cuda.reset_peak_memory_stats()
        baseline_rss = peak_rss_bytes()

        began = time.perf_counter()
        assert await runtime.ensure_ready() is True
        startup_seconds = time.perf_counter() - began

        health = runtime.health()
        assert health.status == "ready"
        assert health.reason is None
        fingerprint = health.fingerprint
        assert fingerprint is not None
        assert fingerprint.dimension == 1024
        assert fingerprint.device == settings.model_device
        assert fingerprint.dtype == settings.model_dtype
        rss_after_start = peak_rss_bytes()

        began = time.perf_counter()
        cold_embeddings = await runtime.inference.embed([QUERY])
        embed_cold_seconds = time.perf_counter() - began
        began = time.perf_counter()
        hot_embeddings = await runtime.inference.embed([QUERY])
        embed_hot_seconds = time.perf_counter() - began
        for vector in (cold_embeddings[0], hot_embeddings[0]):
            assert len(vector) == 1024
            assert math.isclose(norm(vector), 1.0, rel_tol=1e-4, abs_tol=1e-4)

        began = time.perf_counter()
        cold_scores = await runtime.inference.rerank(QUERY, [PASSAGE])
        rerank_cold_seconds = time.perf_counter() - began
        began = time.perf_counter()
        hot_scores = await runtime.inference.rerank(QUERY, [PASSAGE])
        rerank_hot_seconds = time.perf_counter() - began
        assert math.isfinite(cold_scores[0])
        assert math.isfinite(hot_scores[0])

        report = {
            "platform": sys.platform,
            "python": sys.version.split()[0],
            "device": settings.model_device,
            "dtype": settings.model_dtype,
            "fingerprint": asdict(fingerprint),
            "startup_seconds": round(startup_seconds, 3),
            "embed_cold_seconds": round(embed_cold_seconds, 3),
            "embed_hot_seconds": round(embed_hot_seconds, 3),
            "rerank_cold_seconds": round(rerank_cold_seconds, 3),
            "rerank_hot_seconds": round(rerank_hot_seconds, 3),
            "baseline_peak_rss_bytes": baseline_rss,
            "peak_rss_after_start_bytes": rss_after_start,
            "peak_rss_after_inference_bytes": peak_rss_bytes(),
            **gpu_report(settings.model_device),
        }
        print("RUNTIME_REPORT " + json.dumps(report, sort_keys=True))
        record_property("runtime_report", json.dumps(report, sort_keys=True))
    finally:
        await runtime.close()
    assert runtime.health().status == "stopped"


class GatedLoader(_SentenceTransformerLoader):
    """Real loader that gates one physical embed to prove cancellation waits."""

    def __init__(self) -> None:
        self.gate_next = False
        self.blocked = threading.Event()
        self.release = threading.Event()

    def embed(self, model, text):
        if self.gate_next:
            self.gate_next = False
            self.blocked.set()
            if not self.release.wait(300):
                raise TimeoutError("Test gate was not released")
        return super().embed(model, text)


async def test_cancelled_inference_waits_for_real_physical_thread(
    workspace_tmp_path,
):
    loader = GatedLoader()
    inference = LocalInference(runtime_settings(workspace_tmp_path), loader=loader)
    await inference.start()
    try:
        loader.gate_next = True
        task = asyncio.create_task(inference.embed([QUERY]))
        assert await asyncio.to_thread(loader.blocked.wait, 300)
        task.cancel()
        await asyncio.sleep(0.2)
        # The coroutine stays pending while real inference is physically alive.
        assert task.done() is False
        loader.release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        vectors = await inference.embed([QUERY])
        assert len(vectors) == 1
        assert len(vectors[0]) == 1024
    finally:
        loader.release.set()
        await inference.drain(timeout=300)


def start_owner(settings: Settings, events: Path, release: Path, seconds: float):
    return subprocess.Popen(
        [
            sys.executable,
            str(OWNER_SCRIPT),
            "hold",
            str(settings.model_lock_path),
            str(events),
            str(release),
            str(seconds),
        ],
        cwd=BACKEND,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def wait_for(events: Path, token: str, timeout: float = 30) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        text = events.read_text(encoding="utf-8") if events.exists() else ""
        if token in text:
            return
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for {token!r}")


def stop_owner(process) -> None:
    if process.poll() is None:
        process.terminate()
        process.communicate(timeout=15)


def nt_process_call(pid: int, name: str) -> int:
    """Call an undocumented ntdll process operation on a child test process."""

    from ctypes import wintypes

    process_suspend_resume = 0x0800
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    ntdll = ctypes.WinDLL("ntdll")
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    function = getattr(ntdll, name, None)
    if function is None:
        pytest.skip(f"{name} unavailable; suspended owner NOT EXECUTED")
    function.argtypes = [wintypes.HANDLE]
    function.restype = ctypes.c_long
    handle = kernel32.OpenProcess(process_suspend_resume, False, pid)
    if not handle:
        pytest.skip(
            f"OpenProcess failed ({ctypes.get_last_error()}); suspended owner "
            "NOT EXECUTED"
        )
    try:
        return int(function(handle))
    finally:
        kernel32.CloseHandle(handle)


@pytest.mark.skipif(
    os.name != "nt",
    reason="Windows owner suspension/termination NOT EXECUTED on this platform",
)
async def test_windows_suspended_and_terminated_owner(workspace_tmp_path):
    settings = runtime_settings(workspace_tmp_path)
    events = workspace_tmp_path / "windows-events.txt"
    release = workspace_tmp_path / "windows-release"
    process = start_owner(settings, events, release, 300)
    runtime = None
    try:
        wait_for(events, "owner")
        assert ModelOwner(settings.model_lock_path).acquire() is False

        status = nt_process_call(process.pid, "NtSuspendProcess")
        if status != 0:
            pytest.skip(
                f"NtSuspendProcess failed ({status:#x}); suspended owner NOT EXECUTED"
            )
        try:
            assert ModelOwner(settings.model_lock_path).acquire() is False
        finally:
            resumed = nt_process_call(process.pid, "NtResumeProcess")
            assert resumed == 0

        process.kill()
        process.communicate(timeout=15)
        assert process.returncode != 0

        owner = ModelOwner(settings.model_lock_path)
        assert owner.acquire() is True
        owner.close()

        # A new owner starts the real runtime after the previous one died.
        runtime = ModelRuntime(runtime_settings(workspace_tmp_path))
        assert await runtime.ensure_ready() is True
        assert runtime.health().status == "ready"
    finally:
        if runtime is not None:
            await runtime.close()
        stop_owner(process)


@pytest.mark.skipif(
    sys.platform != "linux",
    reason="Linux owner suspension/termination NOT EXECUTED on this platform",
)
async def test_linux_suspended_and_terminated_owner(workspace_tmp_path):
    settings = runtime_settings(workspace_tmp_path)
    events = workspace_tmp_path / "linux-events.txt"
    release = workspace_tmp_path / "linux-release"
    process = start_owner(settings, events, release, 300)
    runtime = None
    try:
        wait_for(events, "owner")
        assert ModelOwner(settings.model_lock_path).acquire() is False

        os.kill(process.pid, signal.SIGSTOP)
        try:
            assert ModelOwner(settings.model_lock_path).acquire() is False
        finally:
            os.kill(process.pid, signal.SIGCONT)

        os.kill(process.pid, signal.SIGKILL)
        process.communicate(timeout=15)

        owner = ModelOwner(settings.model_lock_path)
        assert owner.acquire() is True
        owner.close()

        runtime = ModelRuntime(runtime_settings(workspace_tmp_path))
        assert await runtime.ensure_ready() is True
        assert runtime.health().status == "ready"
    finally:
        if runtime is not None:
            await runtime.close()
        stop_owner(process)
