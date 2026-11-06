import subprocess
import sys
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
SCRIPT = BACKEND / "tests" / "support" / "model_process.py"


def _start(lock: Path, events: Path, release: Path, seconds: float):
    return subprocess.Popen(
        [
            sys.executable,
            str(SCRIPT),
            "hold",
            str(lock),
            str(events),
            str(release),
            str(seconds),
        ],
        cwd=BACKEND,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _wait_for(events: Path, token: str, count: int = 1, timeout: float = 15) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        text = events.read_text(encoding="utf-8") if events.exists() else ""
        if text.count(token) >= count:
            return
        time.sleep(0.02)
    text = events.read_text(encoding="utf-8") if events.exists() else ""
    raise AssertionError(f"timed out waiting for {token!r} in {text!r}")


def _stop(process, release: Path) -> None:
    release.touch()
    try:
        process.communicate(timeout=15)
    finally:
        if process.poll() is None:
            process.terminate()
            process.communicate(timeout=15)


def test_second_owner_never_loads_models(workspace_tmp_path):
    lock = workspace_tmp_path / "model.lock"
    events = workspace_tmp_path / "events.txt"
    first_release = workspace_tmp_path / "first.release"
    second_release = workspace_tmp_path / "second.release"
    first = _start(lock, events, first_release, 30)
    second = None
    try:
        _wait_for(events, "loaded")
        second = _start(lock, events, second_release, 30)
        _, error = second.communicate(timeout=15)
        assert second.returncode == 0, error
        text = events.read_text(encoding="utf-8")
        assert "busy" in text
        assert text.count("load_embedding") == 1
        assert text.count("load_reranker") == 1
        assert text.count("loaded") == 1
    finally:
        if second is not None and second.poll() is None:
            second.terminate()
            second.communicate(timeout=15)
        _stop(first, first_release)
    assert "released" in events.read_text(encoding="utf-8")


def test_owner_is_reacquired_after_clean_release(workspace_tmp_path):
    lock = workspace_tmp_path / "model.lock"
    events = workspace_tmp_path / "events.txt"
    first_release = workspace_tmp_path / "first.release"
    second_release = workspace_tmp_path / "second.release"
    first = _start(lock, events, first_release, 30)
    second = None
    try:
        _wait_for(events, "loaded")
        _stop(first, first_release)
        _wait_for(events, "released")
        second = _start(lock, events, second_release, 30)
        _wait_for(events, "loaded", count=2)
    finally:
        if second is not None and second.poll() is None:
            _stop(second, second_release)
    text = events.read_text(encoding="utf-8")
    assert text.count("load_embedding") == 2
    assert text.count("busy") == 0


def test_terminated_owner_releases_lock(workspace_tmp_path):
    lock = workspace_tmp_path / "model.lock"
    events = workspace_tmp_path / "events.txt"
    first_release = workspace_tmp_path / "first.release"
    second_release = workspace_tmp_path / "second.release"
    first = _start(lock, events, first_release, 30)
    second = None
    try:
        _wait_for(events, "loaded")
        first.terminate()
        first.communicate(timeout=15)
        assert first.returncode != 0
        second = _start(lock, events, second_release, 30)
        _wait_for(events, "loaded", count=2)
        assert "released" not in events.read_text(encoding="utf-8")
    finally:
        if second is not None and second.poll() is None:
            _stop(second, second_release)
