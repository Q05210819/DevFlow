"""Bounded, synchronous Git operations in a fixed, independently owned supervisor.

Callers run this blocking interface in their owned worker thread and propagate
cancellation until the operation exits. Durable admission is supplied by Tasks
2–4; absent durable activation fails closed, never falls back to memory.
"""

import base64
import json
import math
import os
import queue
import re
import signal
import stat
import struct
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path

MAX_OUTPUT = 16 * 1024 * 1024
MAX_FRAME = 24 * 1024 * 1024
CLEANUP_SECONDS = 2.0
_WRITERS = threading.BoundedSemaphore(1)
_READERS = threading.BoundedSemaphore(4)
_OID = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?\Z")
_ATTEMPT = re.compile(r"refs/devflow/(?:attempts|pins)/[0-9a-f]{32}\Z")


class WorkspaceProcessError(Exception):
    """A finite safe error code, never provider output or command arguments."""


@dataclass(frozen=True)
class ProcessResult:
    status: str
    stdout: bytes = field(default=b"", repr=False)
    returncode: int | None = None


@dataclass(frozen=True)
class GuardRecord:
    operation_id: str
    nonce: str
    repository_id: int
    kind: str
    lock_path: Path
    database_url: str = field(repr=False)


def _environment(executable):
    """No inherited Git, proxy, credential, SSH or executable override variables."""
    env = {"PATH": str(Path(executable).parent), "LC_ALL": "C"}
    if os.name == "nt":
        system = os.environ.get("SystemRoot", r"C:\Windows")
        env["SystemRoot"] = system
        env["PATH"] += os.pathsep + str(Path(system) / "System32")
    env.update(
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_SYSTEM=os.devnull,
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_ATTR_NOSYSTEM="1",
        GIT_TERMINAL_PROMPT="0",
        GIT_NO_REPLACE_OBJECTS="1",
        GIT_NO_LAZY_FETCH="1",
        GIT_OPTIONAL_LOCKS="0",
        GIT_LITERAL_PATHSPECS="1",
        GIT_PROTOCOL_FROM_USER="0",
    )
    return env


def _git_prefix():
    config = {
        "protocol.allow": "never",
        "protocol.https.allow": "always",
        "http.followRedirects": "false",
        "http.sslVerify": "true",
        "http.proxy": "",
        "credential.helper": "",
        "credential.interactive": "false",
        "core.hooksPath": os.devnull,
        "core.fsmonitor": "false",
        "maintenance.auto": "false",
        "gc.auto": "0",
        "fetch.recurseSubmodules": "false",
        "submodule.recurse": "false",
        "fetch.fsckObjects": "true",
        "transfer.fsckObjects": "true",
    }
    return [part for key, value in config.items() for part in ("-c", f"{key}={value}")]


def _validate_command(args, env):
    """Only the fixed GitCache command forms cross the supervisor boundary."""
    if (
        not isinstance(args, list)
        or not args
        or any(not isinstance(x, str) or "\0" in x for x in args)
    ):
        return False
    if env and (
        set(env) != {"DEVFLOW_GIT_AUTH"} or not isinstance(env["DEVFLOW_GIT_AUTH"], str)
    ):
        return False
    if args in (["--version"], ["init", "--bare", "--template="]):
        return not env
    if args in (
        ["repack", "-Ad"],
        ["prune", "--expire=now"],
        ["cat-file", "--batch-all-objects", "--batch-check=%(objectname)"],
    ):
        return not env
    if len(args) == 4 and args[:3] == ["show-ref", "--verify", "--hash"]:
        return not env and bool(_ATTEMPT.fullmatch(args[3]))
    if len(args) == 5 and args[:3] == ["update-ref", "--no-deref", "-d"]:
        return (
            not env
            and bool(_ATTEMPT.fullmatch(args[3]))
            and bool(_OID.fullmatch(args[4]))
        )
    if (
        len(args) == 3
        and args[0] == "cat-file"
        and args[1] in {"-t", "-s", "blob", "commit", "tag"}
    ):
        return not env and bool(_OID.fullmatch(args[2]))
    if len(args) == 5 and args[:4] == ["ls-tree", "-r", "-z", "--full-tree"]:
        return not env and bool(_OID.fullmatch(args[4]))
    if len(args) == 4 and args[:3] == ["fsck", "--connectivity-only", "--no-dangling"]:
        return not env and bool(_OID.fullmatch(args[3]))
    if len(args) == 4 and args[:3] == ["show-ref", "--verify", "--quiet"]:
        return not env and bool(_ATTEMPT.fullmatch(args[3]))
    if len(args) == 5 and args[:2] == ["update-ref", "--no-deref"]:
        return (
            not env
            and bool(_ATTEMPT.fullmatch(args[2]))
            and bool(_OID.fullmatch(args[3]))
            and args[4] == "0" * len(args[3])
        )
    if len(args) == 9 and args[:7] == [
        "fetch",
        "--depth=1",
        "--no-tags",
        "--no-write-fetch-head",
        "--no-recurse-submodules",
        "--no-auto-maintenance",
        "--force",
    ]:
        from .git import valid_full_name, valid_ref

        remote = re.fullmatch(r"https://github\.com/([^/]+/[^/]+)\.git", args[7])
        source, sep, target = args[8].removeprefix("+").partition(":")
        return bool(
            remote
            and valid_full_name(remote[1])
            and sep
            and valid_ref(source)
            and re.fullmatch(r"refs/devflow/attempts/[0-9a-f]{32}", target)
            and set(env) == {"DEVFLOW_GIT_AUTH"}
        )
    return False


def _write_frame(pipe, message):
    payload = json.dumps(message, separators=(",", ":"), allow_nan=False).encode()
    if len(payload) > MAX_FRAME:
        raise WorkspaceProcessError("protocol_error")
    pipe.write(struct.pack("!I", len(payload)) + payload)
    pipe.flush()


def _read_exact(pipe, length):
    data = bytearray()
    while len(data) < length:
        part = pipe.read(length - len(data))
        if not part:
            raise EOFError
        data.extend(part)
    return bytes(data)


def _read_frame(pipe, maximum):
    size = struct.unpack("!I", _read_exact(pipe, 4))[0]
    if size > maximum:
        raise WorkspaceProcessError("protocol_error")
    return json.loads(_read_exact(pipe, size))


def _read_messages(pipe, messages, ended, maximum):
    try:
        while True:
            message = _read_frame(pipe, maximum)
            while not ended.is_set():
                try:
                    messages.put(message, timeout=0.02)
                    break
                except queue.Full:
                    continue
    except (EOFError, OSError, ValueError, WorkspaceProcessError):
        ended.set()


def _reason(cancel, deadline):
    if cancel.is_set():
        return "cancelled"
    if time.monotonic() >= deadline:
        return "timeout"
    return None


class GitRunner:
    def __init__(self, settings):
        self.executable = Path(settings.workspace_git_executable)
        if not self.executable.is_absolute():
            raise WorkspaceProcessError("invalid_configuration")
        self._local = threading.local()
        self.repository_bytes = settings.workspace_repository_bytes
        self.total_bytes = settings.workspace_total_bytes

    def _supervisor_command(self):
        return [sys.executable, "-m", "devflow.workspace.process", "--supervisor"]

    @property
    def current(self):
        operation = getattr(self._local, "operation", None)
        if operation is None:
            raise WorkspaceProcessError("guard_required")
        return operation

    @contextmanager
    def operation(self, guard, *, deadline, cancel):
        if getattr(self._local, "operation", None) is not None:
            raise WorkspaceProcessError("operation_nested")
        if (
            not isinstance(guard, GuardRecord)
            or guard.kind not in {"read", "write", "gc"}
            or not math.isfinite(deadline)
            or deadline > time.monotonic() + 600
            or type(guard.repository_id) is not int
            or guard.repository_id <= 0
            or not guard.lock_path.is_absolute()
            or not re.fullmatch(r"[0-9a-f]{32}", guard.operation_id)
            or not re.fullmatch(r"[0-9a-f]{32}", guard.nonce)
        ):
            raise WorkspaceProcessError("invalid_input")
        slot = _READERS if guard.kind == "read" else _WRITERS
        while True:
            reason = _reason(cancel, deadline)
            if reason:
                raise WorkspaceProcessError(reason)
            if slot.acquire(timeout=min(0.02, max(0, deadline - time.monotonic()))):
                break
        session = None
        try:
            session = _Operation(self, guard, deadline, cancel)
            self._local.operation = session
            session.start()
            yield session
        finally:
            self._local.operation = None
            if session is not None:
                session.close()
            # Uncertain active cleanup deliberately retains the local slot too.
            if session is None or session.drained or not session.active:
                slot.release()

    def run(self, args, *, cwd, env, timeout, max_output, cancel, record_limit=None):
        operation = self.current
        if (
            not 0 < max_output <= MAX_OUTPUT
            or not math.isfinite(timeout)
            or timeout <= 0
            or record_limit is not None
            and not 0 < record_limit <= 100000
        ):
            raise WorkspaceProcessError("invalid_input")
        return operation.run(
            args, Path(cwd), env, timeout, max_output, cancel, record_limit
        )

    def bootstrap(self):
        """Initialize only this operation's numeric repository under its lock."""
        return self.current.bootstrap()


class _Operation:
    def __init__(self, runner, guard, deadline, cancel):
        self.runner, self.guard = runner, guard
        self.deadline, self.cancel = deadline, cancel
        self.process = None
        # The OS process id of the supervisor interpreter itself. On Windows a
        # venv launcher may spawn the real interpreter as a child, so this can
        # differ from ``self.process.pid``.
        self.native_pid = None
        self.messages = queue.Queue(maxsize=2)
        self.ended = threading.Event()
        self.active = False
        self.drained = False
        self.closed_input = False

    def start(self):
        env = _environment(sys.executable)
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2])
        try:
            self.process = subprocess.Popen(
                self.runner._supervisor_command(),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=env,
                cwd=Path(__file__).resolve().parents[2],
                bufsize=0,
                close_fds=True,
                start_new_session=os.name != "nt",
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            self.reader = threading.Thread(
                target=_read_messages,
                args=(self.process.stdout, self.messages, self.ended, MAX_FRAME),
                daemon=True,
            )
            self.reader.start()
            guard = asdict(self.guard)
            guard["lock_path"] = str(self.guard.lock_path)
            self.send(
                {
                    "guard": guard,
                    "deadline": self.deadline,
                    "executable": str(self.runner.executable),
                    "budgets": [self.runner.repository_bytes, self.runner.total_bytes],
                }
            )
            # After sending activation, loss of its reply is ambiguous.
            self.active = True
            result = self.receive(self.cancel)
            self.native_pid = result.get("pid") or self.process.pid
            self.active = result.get("active", True)
            self.drained = result.get("drained", False)
            if result.get("status") != "ready":
                raise WorkspaceProcessError(result.get("status", "cleanup_required"))
        except OSError:
            raise WorkspaceProcessError("spawn_failed") from None

    def send(self, message):
        if self.closed_input:
            raise WorkspaceProcessError("cancelled")
        if len(json.dumps(message)) > 65536:
            raise WorkspaceProcessError("invalid_input")
        try:
            _write_frame(self.process.stdin, message)
        except (OSError, ValueError):
            raise WorkspaceProcessError("cleanup_required") from None

    def cancel_input(self):
        if not self.closed_input and self.process:
            self.closed_input = True
            self.process.stdin.close()

    def receive(self, cancel):
        limit = self.deadline + CLEANUP_SECONDS
        while time.monotonic() < limit:
            if cancel.is_set():
                self.cancel_input()
            try:
                return self.messages.get(timeout=0.02)
            except queue.Empty:
                if self.ended.is_set():
                    break
        raise WorkspaceProcessError("cleanup_required")

    def run(self, args, cwd, env, timeout, max_output, cancel, record_limit):
        reason = _reason(cancel, self.deadline)
        if reason:
            return ProcessResult(reason)
        self.send(
            {
                "op": "run",
                "args": args,
                "cwd": str(cwd),
                "env": env,
                "deadline": min(self.deadline, time.monotonic() + timeout),
                "max_output": max_output,
                "record_limit": record_limit,
            }
        )
        result = self.receive(cancel)
        if result.get("op") != "result":
            if result.get("op") == "closed":
                self.drained = result.get("drained", False)
                self.active = result.get("active", self.active)
                self.cancel_input()
            raise WorkspaceProcessError("cleanup_required")
        return ProcessResult(
            result["status"],
            base64.b64decode(result.get("stdout", "")),
            result.get("returncode"),
        )

    def bootstrap(self):
        self.send({"op": "bootstrap"})
        result = self.receive(self.cancel)
        if result.get("op") != "result":
            if result.get("op") == "closed":
                self.drained = result.get("drained", False)
                self.active = result.get("active", self.active)
                self.cancel_input()
            raise WorkspaceProcessError("cleanup_required")
        return ProcessResult(result["status"], returncode=result.get("returncode"))

    def close(self):
        if self.process is None:
            return
        try:
            if not self.drained:
                if not self.closed_input and not self.ended.is_set():
                    try:
                        self.send({"op": "finish"})
                    except WorkspaceProcessError:
                        # A terminal operation can close stdin before its final
                        # proof is delivered by the reader. Still consume proof.
                        pass
                    self.cancel_input()
                while True:
                    result = self.receive(threading.Event())
                    if result.get("op") == "closed":
                        self.drained = result.get("drained", False)
                        self.active = result.get("active", self.active)
                        break
            self.process.wait(
                timeout=max(0, self.deadline + CLEANUP_SECONDS - time.monotonic())
            )
        except (WorkspaceProcessError, subprocess.TimeoutExpired, OSError):
            # Never turn a missing cleanup acknowledgement into slot release.
            pass
        finally:
            self.cancel_input()
            if self.process.poll() is not None:
                self.reader.join(timeout=0.2)
                self.process.stdout.close()
        if self.active and not self.drained:
            raise WorkspaceProcessError("cleanup_required")


def _activate_guard(guard, identity):
    try:
        from .guards import activate_guard
    except ImportError:
        raise WorkspaceProcessError("guard_unavailable") from None
    return activate_guard(guard, identity)


def _drain_guard(guard):
    from .guards import drain_guard

    return drain_guard(guard)


def _identity(job_name):
    if os.name == "nt":
        from .windows import creation_identity

        return {
            "platform": "windows",
            "pid": os.getpid(),
            "creation": creation_identity(),
            "job": job_name,
        }
    import ctypes

    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(36, 1, 0, 0, 0) != 0:  # PR_SET_CHILD_SUBREAPER
        raise WorkspaceProcessError("containment_failed")
    stat = Path("/proc/self/stat").read_text().rsplit(")", 1)[1].split()
    return {
        "platform": "linux",
        "pid": os.getpid(),
        "creation": stat[19],
        "session": os.getsid(0),
        "boot": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
        "pid_namespace": os.stat("/proc/self/ns/pid").st_ino,
    }


class _RepoLock:
    def __init__(self, path, shared):
        self.file = open(path, "rb", buffering=0)  # noqa: SIM115 - operation owns close()
        if os.name == "nt":
            from .windows import FileLock

            self.native = FileLock(self.file, shared)
        else:
            import fcntl

            self.flags = (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB
        self.held = False

    def acquire(self):
        if os.name == "nt":
            self.held = self.native.acquire()
        else:
            import fcntl

            try:
                fcntl.flock(self.file.fileno(), self.flags)
                self.held = True
            except BlockingIOError:
                return False
        return self.held

    def close(self):
        if self.held:
            if os.name == "nt":
                self.native.release()
            else:
                import fcntl

                fcntl.flock(self.file.fileno(), fcntl.LOCK_UN)
        self.file.close()


def _capture(pipe, limit, overflow, output=None, record_limit=None):
    size = records = 0
    try:
        while chunk := pipe.read(65536):
            original = len(chunk)
            if record_limit is not None:
                available = record_limit - records
                count = chunk.count(b"\0")
                if count > available:
                    # Keep only complete records; count is enforced during drain.
                    end = 0
                    for _ in range(available):
                        end = chunk.index(b"\0", end) + 1
                    chunk = chunk[:end]
                    overflow.set()
                records += min(count, available)
            if size + len(chunk) > limit:
                chunk = chunk[: max(0, limit - size)]
                overflow.set()
            if output is not None:
                output.extend(chunk)
            size += len(chunk)
            if size >= limit and original > len(chunk):
                overflow.set()
    finally:
        pipe.close()


def _kill_group(pid, sig):
    try:
        os.killpg(pid, sig)
    except ProcessLookupError:
        pass


def _drain_tree(process, job, deadline):
    if os.name == "nt":
        job.terminate()
        while time.monotonic() < deadline:
            if job.active() == 0 and process.poll() is not None:
                return True
            time.sleep(0.01)
        return False
    _kill_group(process.pid, signal.SIGTERM)
    graceful = min(deadline, time.monotonic() + 0.1)
    while process.poll() is None and time.monotonic() < graceful:
        time.sleep(0.01)
    _kill_group(process.pid, signal.SIGKILL)
    while time.monotonic() < deadline:
        process.poll()
        if process.returncode is not None:
            try:
                while os.waitpid(-1, os.WNOHANG)[0]:
                    pass
            except ChildProcessError:
                pass
            try:
                os.killpg(process.pid, 0)
            except ProcessLookupError:
                # Subreaper adoption must also be empty: a surviving helper in
                # another group is not proven dead by absence of the root PGID.
                children = Path(f"/proc/self/task/{os.getpid()}/children").read_text()
                if not children.strip():
                    return True
        time.sleep(0.01)
    return False


def _git_argv(executable, request):
    argv = [executable, *_git_prefix()]
    if request["env"]:
        argv.append("--config-env=http.extraHeader=DEVFLOW_GIT_AUTH")
    argv.extend(request["args"])
    return argv


def storage_usage(root, repository_id, *, deadline):
    """Count every owned-root byte, including unknown and failed attempts."""
    root = Path(root)
    if root.resolve() != root:
        raise WorkspaceProcessError("cache_invalid")
    total = repository = count = 0
    pending = [root]
    while pending:
        if time.monotonic() >= deadline:
            raise WorkspaceProcessError("timeout")
        path = pending.pop()
        try:
            info = path.lstat()
        except FileNotFoundError:
            if path == root:
                return 0, 0
            continue  # concurrent atomic pack replacement; monitor is a soft limit
        except OSError:
            raise WorkspaceProcessError("cache_unavailable") from None
        count += 1
        if (
            count > 100000
            or stat.S_ISLNK(info.st_mode)
            or getattr(info, "st_file_attributes", 0) & 0x400
        ):
            raise WorkspaceProcessError("cache_invalid")
        if stat.S_ISDIR(info.st_mode):
            try:
                with os.scandir(path) as entries:
                    for entry in entries:
                        pending.append(Path(entry.path))
                        if len(pending) + count > 100000:
                            raise WorkspaceProcessError("cache_invalid")
            except OSError:
                raise WorkspaceProcessError("cache_unavailable") from None
        elif stat.S_ISREG(info.st_mode):
            total += info.st_size
            if path.relative_to(root).parts[0] == f"{repository_id}.git":
                repository += info.st_size
        else:
            raise WorkspaceProcessError("cache_invalid")
    return repository, total


def check_storage(root, repository_id, limits, deadline):
    repository, total = storage_usage(root, repository_id, deadline=deadline)
    if repository >= limits[0] or total >= limits[1]:
        raise WorkspaceProcessError("storage_exhausted")


def _guard_command(guard, request):
    """Maintenance cannot borrow a write/read operation's command authority."""
    args = request["args"]
    root = Path(guard["lock_path"]).parent.parent
    repo = root / f"{guard['repository_id']}.git"
    cwd = Path(request["cwd"])
    if cwd != repo or cwd.resolve() != repo or root.resolve() != root:
        return False
    if guard["kind"] == "gc":
        return not request["env"] and (
            args[:3] == ["show-ref", "--verify", "--quiet"]
            or args[:3] == ["show-ref", "--verify", "--hash"]
            or args[:2] == ["cat-file", "-t"]
            or args
            == ["cat-file", "--batch-all-objects", "--batch-check=%(objectname)"]
            or args[:3] == ["update-ref", "--no-deref", "-d"]
            or args in (["repack", "-Ad"], ["prune", "--expire=now"])
        )
    if args[0] in {"prune", "repack"} or args[:3] == ["update-ref", "--no-deref", "-d"]:
        return False
    return not (guard["kind"] == "read" and args[0] in {"fetch", "update-ref", "init"})


def _run_tree(executable, request, stopped, operation_deadline, job, build_argv):
    reason = _reason(stopped, min(operation_deadline, request["deadline"]))
    if reason:
        return ProcessResult(reason), True
    output = bytearray()
    overflow = threading.Event()
    env = _environment(executable)
    env.update(request["env"])
    argv = build_argv(executable, request)
    process = None
    readers = []
    try:
        if os.name == "nt":
            process = job.spawn(argv, request["cwd"], env)
        else:
            process = subprocess.Popen(
                argv,
                cwd=request["cwd"],
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
                close_fds=True,
                process_group=0,
            )
        for pipe, target, records in (
            (process.stdout, output, request.get("record_limit")),
            (process.stderr, None, None),
        ):
            thread = threading.Thread(
                target=_capture,
                args=(pipe, request["max_output"], overflow, target, records),
                daemon=True,
            )
            thread.start()
            readers.append(thread)
        status = "ok"
        deadline = min(operation_deadline, request["deadline"])
        next_budget = 0
        while True:
            if stopped.is_set():
                status = "cancelled"
                break
            if overflow.is_set():
                status = "output_limit"
                break
            if time.monotonic() >= deadline - 0.05:
                status = "timeout"
                break
            if request.get("_budget") and time.monotonic() >= next_budget:
                root, numeric, limits = request["_budget"]
                try:
                    check_storage(root, numeric, limits, deadline)
                except WorkspaceProcessError as error:
                    status = str(error)
                    break
                next_budget = time.monotonic() + 0.2
            code = process.poll()
            if code is not None:
                status = "ok" if code == 0 else "git_failed"
                break
            time.sleep(0.01)
        cleanup_deadline = time.monotonic() + CLEANUP_SECONDS
        request["_cleanup_deadline"] = cleanup_deadline
        proven = _drain_tree(process, job, cleanup_deadline)
        for reader in readers:
            reader.join(timeout=max(0, cleanup_deadline - time.monotonic()))
        proven = proven and all(not reader.is_alive() for reader in readers)
        if overflow.is_set() and status in {"ok", "git_failed"}:
            status = "output_limit"
        if not proven:
            return ProcessResult("cleanup_required"), False
        return ProcessResult(status, bytes(output), process.poll()), True
    except OSError:
        cleanup_deadline = time.monotonic() + CLEANUP_SECONDS
        request["_cleanup_deadline"] = cleanup_deadline
        if process is not None:
            proven = _drain_tree(process, job, cleanup_deadline)
        else:
            # Native creation may fail after the suspended process was created.
            proven = os.name != "nt" or job.active() == 0
        return ProcessResult("spawn_failed" if proven else "cleanup_required"), proven
    finally:
        if os.name == "nt" and process is not None:
            process.close()


def _bootstrap_cache(executable, guard, request, stopped, deadline, job, build_argv):
    """Fixed bootstrap only; paths/config are derived, never supplied as writes."""
    from .git import _CONFIG

    try:
        repository_id = guard["repository_id"]
        lock_path = Path(guard["lock_path"])
        if (
            guard["kind"] != "write"
            or type(repository_id) is not int
            or not 0 < repository_id < 2**63
            or not lock_path.is_absolute()
            or lock_path.parent.name != ".locks"
            or lock_path.name != f"{repository_id}.lock"
            or lock_path.resolve() != lock_path
        ):
            raise WorkspaceProcessError("invalid_input")
        repo = lock_path.parent.parent / f"{repository_id}.git"
        if repo.resolve() != repo:
            raise WorkspaceProcessError("invalid_input")
        reason = _reason(stopped, deadline)
        if reason:
            return ProcessResult(reason), True
        if repo.exists():
            return ProcessResult("ok", returncode=0), True
        repo.mkdir()
    except OSError:
        # These synchronous filesystem phases precede any child launch.
        return ProcessResult("cache_invalid"), True
    init = {
        "args": ["init", "--bare", "--template="],
        "cwd": str(repo),
        "env": {},
        "deadline": deadline,
        "max_output": 4096,
    }
    result, proven = _run_tree(executable, init, stopped, deadline, job, build_argv)
    request["_cleanup_deadline"] = init.get("_cleanup_deadline")
    if proven and result.status == "ok":
        reason = _reason(stopped, deadline)
        if reason:
            return ProcessResult(reason), True
        # This is the same supervisor thread that owns/releases the OS guard.
        # Even a stalled filesystem call cannot race an independent drain ACK.
        try:
            (repo / "config").write_bytes(_CONFIG)
        except OSError:
            # _run_tree has already proved physical teardown in this branch.
            return ProcessResult("cache_invalid"), True
    return result, proven


def _supervisor_main(
    *,
    activate=_activate_guard,
    drain=_drain_guard,
    validate_command=_validate_command,
    build_argv=_git_argv,
):
    messages = queue.Queue(maxsize=1)
    stopped = threading.Event()
    reader = threading.Thread(
        target=_read_messages,
        args=(sys.stdin.buffer, messages, stopped, 65536),
        daemon=True,
    )
    reader.start()
    guard = lock = job = None
    active = False
    maintenance_layout_checked = False
    proven = True
    cleanup_deadline = None
    status = "guard_unavailable"
    try:
        initial = messages.get(timeout=2)
        guard = initial["guard"]
        deadline = initial["deadline"]
        guard["deadline"] = deadline
        executable = initial["executable"]
        budgets = initial.get("budgets", [2 * 1024**3, 20 * 1024**3])
        if (
            not math.isfinite(deadline)
            or deadline > time.monotonic() + 600
            or not Path(executable).is_absolute()
            or not isinstance(budgets, list)
            or len(budgets) != 2
            or any(type(limit) is not int or limit <= 0 for limit in budgets)
        ):
            raise WorkspaceProcessError("invalid_input")
        job_name = "Local\\DevFlow-" + guard["operation_id"]
        if os.name == "nt":
            from .windows import Job

            job = Job(job_name)
        identity = _identity(job_name)
        if not activate(guard, identity):
            raise WorkspaceProcessError("guard_unavailable")
        active = True
        try:
            lock = _RepoLock(guard["lock_path"], guard["kind"] == "read")
        except OSError:
            raise WorkspaceProcessError("lock_unavailable") from None
        while not lock.acquire():
            reason = _reason(stopped, deadline)
            if reason:
                raise WorkspaceProcessError(reason)
            time.sleep(0.01)
        _write_frame(
            sys.stdout.buffer,
            {"status": "ready", "active": True, "pid": os.getpid()},
        )
        while not _reason(stopped, deadline):
            try:
                request = messages.get(timeout=0.02)
            except queue.Empty:
                continue
            if request.get("op") == "finish":
                break
            if guard["kind"] == "write":
                root = Path(guard["lock_path"]).parent.parent
                try:
                    check_storage(root, guard["repository_id"], budgets, deadline)
                except WorkspaceProcessError as error:
                    _write_frame(
                        sys.stdout.buffer, {"op": "result", "status": str(error)}
                    )
                    break
                if request.get("op") == "run":
                    request["_budget"] = (root, guard["repository_id"], budgets)
            if request.get("op") == "bootstrap" and set(request) == {"op"}:
                proven = False
                try:
                    result, proven = _bootstrap_cache(
                        executable, guard, request, stopped, deadline, job, build_argv
                    )
                except WorkspaceProcessError as exc:
                    result, proven = ProcessResult(str(exc)), True
            elif (
                request.get("op") == "run"
                and validate_command(request["args"], request["env"])
                and _guard_command(guard, request)
                and 0 < request["max_output"] <= MAX_OUTPUT
                and math.isfinite(request["deadline"])
            ):
                if (
                    guard["kind"] == "gc"
                    and request["args"][0] in {"update-ref", "repack", "prune"}
                    and not maintenance_layout_checked
                ):
                    # Git follows nested directory links. Prove the whole owned
                    # layout before the first mutation, under this exclusive OS
                    # guard and on the supervisor thread that acknowledges drain.
                    root = Path(guard["lock_path"]).parent.parent
                    storage_usage(
                        root / f"{guard['repository_id']}.git",
                        guard["repository_id"],
                        deadline=deadline,
                    )
                    maintenance_layout_checked = True
                proven = False
                result, proven = _run_tree(
                    executable, request, stopped, deadline, job, build_argv
                )
            else:
                raise WorkspaceProcessError("invalid_input")
            if not proven or result.status in {"timeout", "cancelled", "output_limit"}:
                cleanup_deadline = request.get("_cleanup_deadline")
            _write_frame(
                sys.stdout.buffer,
                {
                    "op": "result",
                    "status": result.status,
                    "stdout": base64.b64encode(result.stdout).decode(),
                    "returncode": result.returncode,
                },
            )
            if not proven or result.status in {"timeout", "cancelled", "output_limit"}:
                break
        status = "ok"
    except WorkspaceProcessError as exc:
        status = str(exc)
    except (OSError, ValueError, KeyError, TypeError, queue.Empty):
        status = "containment_failed"
    finally:
        drained = not active
        if lock:
            lock.close()
        if job:
            job.close()
        if active and proven:
            try:
                guard["cleanup_deadline"] = min(
                    deadline + CLEANUP_SECONDS,
                    cleanup_deadline or time.monotonic() + CLEANUP_SECONDS,
                )
                drained = bool(drain(guard))
            except Exception:  # noqa: BLE001 - failed DB acknowledgement must retain admission
                drained = False
        try:
            _write_frame(
                sys.stdout.buffer,
                {
                    "op": "closed",
                    "status": status,
                    "active": active,
                    "drained": drained,
                },
            )
        except (OSError, ValueError):
            pass


if __name__ == "__main__" and sys.argv[1:] == ["--supervisor"]:
    _supervisor_main()
