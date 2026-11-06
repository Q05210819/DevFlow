"""Real owned process trees; fixture lifecycle records are never a DB substitute."""

import importlib
import io
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from uuid import uuid4

import pytest


def api():
    try:
        return importlib.import_module("devflow.workspace.process")
    except ModuleNotFoundError:
        pytest.fail("bounded workspace process lifecycle is not implemented")


# This entrypoint injects only test records and a Python fixture executable.
# The installed production entrypoint has neither escape hatch enabled.
SUPERVISOR = """
import json, os
from pathlib import Path
from devflow.workspace.process import _supervisor_main, _git_argv
def activate(guard, identity):
    global diagnostic_path
    path = Path(guard["database_url"])
    diagnostic_path = path.with_suffix(".native_error")
    path.write_text(json.dumps({"state": "active", "identity": identity, "deadline": guard.get("deadline")}))
    return True
def drain(guard):
    path = Path(guard["database_url"])
    state = json.loads(path.read_text())
    state["state"] = "drained"
    state["cleanup_deadline"] = guard.get("cleanup_deadline")
    path.write_text(json.dumps(state))
    return True
def build_argv(executable, request):
    if Path(executable).name.lower().startswith("python"):
        return [executable, *request["args"]]
    return _git_argv(executable, request)
if os.name == "nt":
    from devflow.workspace.windows import Job
    original_spawn = Job.spawn
    def observed_spawn(self, *args):
        try:
            return original_spawn(self, *args)
        except OSError as error:
            diagnostic_path.write_text(str(error))
            raise
    Job.spawn = observed_spawn
_supervisor_main(activate=activate, drain=drain, validate_command=lambda *a: True, build_argv=build_argv)
"""

TREE = """
import os, subprocess, sys, time
from pathlib import Path
folder = Path(sys.argv[1])
role = sys.argv[2]
(folder / (role + ".pid")).write_text(str(os.getpid()))
if role == "root":
    subprocess.Popen([sys.executable, __file__, str(folder), "child"])
elif role == "child":
    subprocess.Popen([sys.executable, __file__, str(folder), "grandchild"])
if role == "grandchild":
    (folder / "ready").write_text("ready")
while not (folder / "ready").exists():
    time.sleep(.01)
if role == "root" and (folder / "exit-root").exists():
    sys.exit(0)
while True:
    time.sleep(1)
"""


@pytest.fixture
def process_setup(workspace_tmp_path, settings_factory, monkeypatch):
    mod = api()
    entry = workspace_tmp_path / "supervisor.py"
    entry.write_text(SUPERVISOR)
    runner = mod.GitRunner(
        settings_factory(workspace_git_executable=Path(sys.executable))
    )
    monkeypatch.setattr(
        runner, "_supervisor_command", lambda: [sys.executable, str(entry)]
    )
    lock = workspace_tmp_path / ".locks" / "1.lock"
    lock.parent.mkdir()
    lock.touch()
    (workspace_tmp_path / "1.git").mkdir()
    guard = mod.GuardRecord(
        operation_id=uuid4().hex,
        nonce=uuid4().hex,
        repository_id=1,
        kind="write",
        lock_path=lock,
        database_url=str(workspace_tmp_path / "guard.json"),
    )
    return runner, guard


def wait_for(predicate, seconds=5):
    deadline = time.monotonic() + seconds
    while not predicate():
        assert time.monotonic() < deadline, "owned fixture handshake timed out"
        time.sleep(0.01)


def alive(pid):
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x100000, False, pid)
        if not handle:
            return False
        try:
            return kernel.WaitForSingleObject(handle, 0) == 258
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


@pytest.mark.parametrize("cause", ["cancel", "deadline", "root_exit"])
def test_entire_tree_is_dead_before_guard_drained(
    process_setup, workspace_tmp_path, cause
):
    runner, guard = process_setup
    script = workspace_tmp_path / "tree.py"
    script.write_text(TREE)
    if cause == "root_exit":
        (workspace_tmp_path / "exit-root").touch()
    cancel = threading.Event()
    results = []

    def execute():
        with runner.operation(guard, deadline=time.monotonic() + 4, cancel=cancel):
            results.append(
                runner.run(
                    [str(script), str(workspace_tmp_path), "root"],
                    cwd=workspace_tmp_path / "1.git",
                    env={},
                    timeout=3 if cause == "deadline" else 4,
                    max_output=1024,
                    cancel=cancel,
                )
            )

    thread = threading.Thread(target=execute)
    thread.start()
    wait_for(lambda: (workspace_tmp_path / "ready").exists())
    pids = [int(p.read_text()) for p in workspace_tmp_path.glob("*.pid")]
    assert len(pids) == 3
    if cause == "cancel":
        cancel.set()
    thread.join(7)
    assert not thread.is_alive()
    assert not any(alive(pid) for pid in pids)
    assert json.loads(Path(guard.database_url).read_text())["state"] == "drained"
    assert (
        results[0].status
        == {"cancel": "cancelled", "deadline": "timeout", "root_exit": "ok"}[cause]
    )


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_output_is_bounded_without_stderr_disclosure(
    process_setup, workspace_tmp_path, stream
):
    runner, guard = process_setup
    with runner.operation(
        guard, deadline=time.monotonic() + 5, cancel=threading.Event()
    ):
        result = runner.run(
            ["-c", f"import sys; sys.{stream}.buffer.write(b'secret-sentinel'*10000)"],
            cwd=workspace_tmp_path / "1.git",
            env={},
            timeout=4,
            max_output=128,
            cancel=threading.Event(),
        )
    assert result.status == "output_limit"
    assert len(result.stdout) <= 128
    assert not hasattr(result, "stderr")
    assert "secret-sentinel" not in repr(result)


def test_production_supervisor_rejects_missing_durable_guard(
    settings_factory, workspace_tmp_path
):
    mod = api()
    runner = mod.GitRunner(
        settings_factory(workspace_git_executable=Path(sys.executable))
    )
    lock = workspace_tmp_path / "lock"
    lock.touch()
    guard = mod.GuardRecord(uuid4().hex, uuid4().hex, 1, "read", lock, "unconfigured")
    with (
        pytest.raises(mod.WorkspaceProcessError, match="guard_unavailable"),
        runner.operation(
            guard, deadline=time.monotonic() + 5, cancel=threading.Event()
        ),
    ):
        pytest.fail("a production operation activated without durable guard storage")


def test_queue_wait_consumes_original_deadline(process_setup, workspace_tmp_path):
    runner, guard = process_setup
    cancel = threading.Event()
    with runner.operation(guard, deadline=time.monotonic() + 5, cancel=cancel):
        started = time.monotonic()
        other = api().GuardRecord(
            uuid4().hex, uuid4().hex, 2, "write", guard.lock_path, "unused"
        )
        # A distinct runner must still share the local writer admission limit.
        from devflow.config import Settings

        second = api().GitRunner(
            Settings(app_env="test", public_base_url="http://localhost")
        )
        with (
            pytest.raises(api().WorkspaceProcessError, match="timeout"),
            second.operation(other, deadline=started + 0.15, cancel=cancel),
        ):
            pytest.fail("second writer entered an occupied global slot")
        assert time.monotonic() - started < 1


def test_guard_repr_does_not_expose_database_url(process_setup):
    _, guard = process_setup
    assert guard.database_url not in repr(guard)


def test_missing_lock_on_read_is_not_created(process_setup, workspace_tmp_path):
    runner, guard = process_setup
    guard = api().GuardRecord(
        guard.operation_id,
        guard.nonce,
        1,
        "read",
        workspace_tmp_path / "absent",
        guard.database_url,
    )
    with (
        pytest.raises(api().WorkspaceProcessError, match="lock_unavailable"),
        runner.operation(
            guard, deadline=time.monotonic() + 3, cancel=threading.Event()
        ),
    ):
        pytest.fail("read operation created its missing guard file")
    assert not guard.lock_path.exists()


def test_repo_lock_excludes_an_independent_process(process_setup, workspace_tmp_path):
    runner, guard = process_setup
    probe = workspace_tmp_path / "probe.py"
    probe.write_text("""
import sys
from devflow.workspace.process import _RepoLock
lock = _RepoLock(sys.argv[1], False)
try:
    print("acquired" if lock.acquire() else "blocked", flush=True)
finally:
    lock.close()
""")
    with runner.operation(
        guard, deadline=time.monotonic() + 5, cancel=threading.Event()
    ):
        result = subprocess.run(
            [sys.executable, str(probe), str(guard.lock_path)],
            capture_output=True,
            timeout=3,
            check=True,
        )
        assert result.stdout.strip() == b"blocked"
    result = subprocess.run(
        [sys.executable, str(probe), str(guard.lock_path)],
        capture_output=True,
        timeout=3,
        check=True,
    )
    assert result.stdout.strip() == b"acquired"


def test_application_parent_death_drains_surviving_supervisor(
    process_setup, workspace_tmp_path
):
    _runner, guard = process_setup
    tree = workspace_tmp_path / "tree.py"
    tree.write_text(TREE)
    parent = workspace_tmp_path / "parent.py"
    parent.write_text("""
from pathlib import Path
import sys, threading, time
from devflow.config import Settings
from devflow.workspace.process import GitRunner, GuardRecord
root = Path(sys.argv[1])
runner = GitRunner(Settings(app_env="test", public_base_url="http://localhost", workspace_git_executable=Path(sys.executable)))
runner._supervisor_command = lambda: [sys.executable, str(root / "supervisor.py")]
guard = GuardRecord(sys.argv[2], sys.argv[3], 1, "write", root / ".locks" / "1.lock", str(root / "guard.json"))
cancel = threading.Event()
with runner.operation(guard, deadline=time.monotonic() + 7, cancel=cancel):
    runner.run([str(root / "tree.py"), str(root), "root"], cwd=root / "1.git", env={}, timeout=7, max_output=1024, cancel=cancel)
""")
    child = subprocess.Popen(
        [
            sys.executable,
            str(parent),
            str(workspace_tmp_path),
            guard.operation_id,
            guard.nonce,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )
    try:
        wait_for(lambda: (workspace_tmp_path / "ready").exists())
        pids = [int(p.read_text()) for p in workspace_tmp_path.glob("*.pid")]
        child.terminate()
        child.wait(timeout=3)

        def drained():
            try:
                return (
                    json.loads(Path(guard.database_url).read_text())["state"]
                    == "drained"
                )
            except (OSError, ValueError):
                return False

        wait_for(drained, seconds=4)
        assert not any(alive(pid) for pid in pids)
    finally:
        if child.poll() is None:
            child.terminate()
            child.wait(timeout=3)


def test_inherited_git_proxy_and_trace_environment_is_removed(
    process_setup, workspace_tmp_path, monkeypatch
):
    runner, guard = process_setup
    for key in (
        "GIT_CONFIG_COUNT",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_TRACE",
        "HTTPS_PROXY",
        "GIT_SSH_COMMAND",
    ):
        monkeypatch.setenv(key, "environment-sentinel")
    with runner.operation(
        guard, deadline=time.monotonic() + 5, cancel=threading.Event()
    ):
        result = runner.run(
            [
                "-c",
                "import os; print('environment-sentinel' in repr(dict(os.environ)))",
            ],
            cwd=workspace_tmp_path / "1.git",
            env={},
            timeout=3,
            max_output=128,
            cancel=threading.Event(),
        )
    assert result.status == "ok"
    assert result.stdout.strip() == b"False"


def test_record_limit_stops_while_consuming_stdout(process_setup, workspace_tmp_path):
    runner, guard = process_setup
    with runner.operation(
        guard, deadline=time.monotonic() + 5, cancel=threading.Event()
    ):
        result = runner.run(
            ["-c", "import sys; sys.stdout.buffer.write(b'entry\\0'*100000)"],
            cwd=workspace_tmp_path / "1.git",
            env={},
            timeout=3,
            max_output=1024,
            record_limit=3,
            cancel=threading.Event(),
        )
    assert result.status == "output_limit"
    assert result.stdout == b"entry\0" * 3


@pytest.mark.parametrize(
    "args,env",
    [
        (["-c", "core.sshCommand=evil", "fetch"], {}),
        (["checkout", "main"], {}),
        (["cat-file", "blob", "HEAD:path"], {}),
        (["--version"], {"GIT_CONFIG_COUNT": "1"}),
    ],
)
def test_production_protocol_rejects_arbitrary_commands(args, env):
    assert api()._validate_command(args, env) is False


@pytest.mark.skipif(os.name != "nt", reason="Windows atomic Job launch")
def test_failed_atomic_job_creation_never_executes_child(
    workspace_tmp_path, monkeypatch
):
    from devflow.workspace import windows

    marker = workspace_tmp_path / "must-not-exist"
    job = windows.Job("Local\\DevFlow-test-" + uuid4().hex)
    monkeypatch.setattr(windows, "_update_attrs", lambda *args: 0)
    try:
        with pytest.raises(OSError):
            job.spawn(
                [
                    sys.executable,
                    "-c",
                    f"from pathlib import Path; Path({str(marker)!r}).touch()",
                ],
                workspace_tmp_path,
                api()._environment(sys.executable),
            )
        assert job.active() == 0
        assert not marker.exists()
    finally:
        job.close()


def test_guard_callbacks_receive_absolute_bounded_deadlines(process_setup):
    runner, guard = process_setup
    deadline = time.monotonic() + 5
    with runner.operation(guard, deadline=deadline, cancel=threading.Event()):
        state = json.loads(Path(guard.database_url).read_text())
        assert state["deadline"] == deadline
    state = json.loads(Path(guard.database_url).read_text())
    assert time.monotonic() < state["cleanup_deadline"] <= deadline + 2


@pytest.mark.skipif(os.name != "nt", reason="Windows partial launch proof")
def test_partial_launch_without_tree_death_cannot_acknowledge_cleanup(
    workspace_tmp_path,
):
    class FailedLaunch:
        def spawn(self, *args):
            raise OSError("native_containment_failed")

        def active(self):
            return 1

    request = {
        "args": [],
        "env": {},
        "cwd": str(workspace_tmp_path),
        "deadline": time.monotonic() + 3,
        "max_output": 128,
    }
    _, proven = api()._run_tree(
        sys.executable,
        request,
        threading.Event(),
        request["deadline"],
        FailedLaunch(),
        lambda *args: [sys.executable],
    )
    assert proven is False


def test_buffered_drain_ack_is_consumed_after_supervisor_pipe_eof():
    from types import SimpleNamespace

    operation = api()._Operation(None, None, time.monotonic() + 2, threading.Event())
    operation.process = SimpleNamespace(
        stdout=io.BytesIO(), poll=lambda: 0, wait=lambda **kwargs: 0
    )
    operation.reader = SimpleNamespace(join=lambda **kwargs: None)
    operation.closed_input = True
    operation.active = True
    operation.messages.put({"op": "closed", "active": True, "drained": True})
    operation.ended.set()
    operation.close()
    assert operation.drained is True


@pytest.mark.parametrize("proof_present", [True, False])
def test_finish_broken_pipe_still_consumes_buffered_drain_proof(proof_present):
    from types import SimpleNamespace

    class ClosedPeer(io.BytesIO):
        def write(self, value):
            raise BrokenPipeError

    operation = api()._Operation(None, None, time.monotonic() + 0.05, threading.Event())
    operation.process = SimpleNamespace(
        stdin=ClosedPeer(), stdout=io.BytesIO(), poll=lambda: 0, wait=lambda **kwargs: 0
    )
    operation.reader = SimpleNamespace(join=lambda **kwargs: None)
    operation.active = True
    if proof_present:
        operation.messages.put({"op": "closed", "active": True, "drained": True})
        operation.close()
        assert operation.drained
        assert operation.messages.empty()
    else:
        with pytest.raises(api().WorkspaceProcessError, match="cleanup_required"):
            operation.close()
        assert not operation.drained


def test_git_executable_requires_absolute_configured_path(settings_factory):
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="must be absolute"):
        settings_factory(workspace_git_executable="git")


@pytest.mark.parametrize("cancelled", [False, True])
def test_supervisor_never_launches_after_cancel_or_expired_deadline(cancelled):
    class MustNotLaunch:
        def spawn(self, *args):
            pytest.fail("Git launched after cancellation/deadline")

    stopped = threading.Event()
    if cancelled:
        stopped.set()
    deadline = time.monotonic() + (2 if cancelled else -1)
    request = {
        "args": ["--version"],
        "env": {},
        "cwd": str(Path.cwd()),
        "deadline": deadline,
        "max_output": 128,
    }
    result, proven = api()._run_tree(
        sys.executable,
        request,
        stopped,
        deadline,
        MustNotLaunch(),
        lambda *args: [sys.executable],
    )
    assert result.status == ("cancelled" if cancelled else "timeout")
    assert proven
