"""Raw Git object fixtures use a test-only local transport boundary."""

import importlib
import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import SecretStr
from test_workspace_process import SUPERVISOR, api


def git_api():
    try:
        return importlib.import_module("devflow.workspace.git")
    except ModuleNotFoundError:
        pytest.fail("raw Git object cache is not implemented")


@pytest.fixture
def objects(workspace_tmp_path, settings_factory, monkeypatch):
    mod = git_api()
    executable = Path(
        shutil.which("git") or "C:/Program Files/Git/cmd/git.exe"
    ).resolve()
    source = workspace_tmp_path / "source"
    source.mkdir()
    clean = {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")}
    clean.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")

    def git(*args, data=None):
        return subprocess.run(
            [str(executable), *args],
            cwd=source,
            env=clean,
            input=data,
            capture_output=True,
            check=True,
        ).stdout.strip()

    git("init", "--bare", "--template=")
    blob = git("hash-object", "-w", "--stdin", data=b"line1\r\nline2\n")
    link = git("hash-object", "-w", "--stdin", data=b"../../secret.txt")
    tree = git(
        "mktree",
        data=(
            b"100644 blob "
            + blob
            + b"\ttext.txt\n120000 blob "
            + link
            + b"\tlink\n160000 commit "
            + b"a" * 40
            + b"\tsubmodule\n"
        ),
    )
    commit = git(
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit-tree",
        tree.decode(),
        "-m",
        "fixture",
    )
    git("update-ref", "refs/heads/main", commit.decode())
    root = workspace_tmp_path / "cache"
    root.mkdir()
    runner = api().GitRunner(settings_factory(workspace_git_executable=executable))
    entry = workspace_tmp_path / "supervisor.py"
    entry.write_text(SUPERVISOR)
    monkeypatch.setattr(
        runner,
        "_supervisor_command",
        lambda: [str(Path(os.sys.executable)), str(entry)],
    )
    # Only this fixture translates the fixed GitHub remote into a local transport.
    real_run = runner.run
    transfers = []

    def local_run(args, **kwargs):
        args = [
            str(source) if a == "https://github.com/team/project.git" else a
            for a in args
        ]
        return real_run(["-c", "protocol.file.allow=always", *args], **kwargs)

    def fixture_transfer(args, **kwargs):
        if args[0] == "fetch":
            transfers.append((list(args), dict(kwargs["env"])))
            destination = kwargs["cwd"]
            shutil.copytree(
                source / "objects", destination / "objects", dirs_exist_ok=True
            )
            source_ref, target_ref = args[-1].removeprefix("+").split(":")
            target = destination / target_ref
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((source / source_ref).read_bytes())
            return api().ProcessResult("ok", returncode=0)
        return real_run(args, **kwargs)

    monkeypatch.setattr(runner, "run", fixture_transfer)
    cache = mod.GitCache(root, runner)

    def guard(kind="write"):
        lock = root / ".locks" / "1.lock"
        lock.parent.mkdir(exist_ok=True)
        lock.touch(exist_ok=True)
        return api().GuardRecord(
            uuid4().hex,
            uuid4().hex,
            1,
            kind,
            lock,
            str(workspace_tmp_path / (uuid4().hex + ".json")),
        )

    return (
        cache,
        guard,
        commit.decode(),
        blob.decode(),
        link.decode(),
        source,
        local_run,
        transfers,
        git,
    )


def fetch(objects, attempt=None):
    cache, guard, *_ = objects
    return cache.fetch(
        1,
        "team/project",
        "refs/heads/main",
        SecretStr("token-sentinel"),
        attempt or uuid4().hex,
        guard=guard(),
        deadline=time.monotonic() + 15,
        cancel=threading.Event(),
    )


def test_raw_tree_blob_modes_and_immutable_attempt_pin(objects):
    cache, guard, commit, blob, link, *_ = objects
    result = fetch(objects)
    assert result.sha == commit
    with cache.operation(
        guard("read"), deadline=time.monotonic() + 5, cancel=threading.Event()
    ):
        entries = cache.tree(1, result.sha)
        assert entries.complete and not entries.truncated
        assert {(e.path, e.mode) for e in entries.entries} == {
            (b"text.txt", "100644"),
            (b"link", "120000"),
            (b"submodule", "160000"),
        }
        assert cache.blob(1, blob, 1024) == b"line1\r\nline2\n"
        assert cache.blob(1, link, 1024) == b"../../secret.txt"
    assert "token-sentinel" not in (cache.root / "1.git" / "config").read_text()
    args, env = objects[7][0]
    assert args[7] == "https://github.com/team/project.git"
    assert "token-sentinel" not in repr(args)
    assert set(env) == {"DEVFLOW_GIT_AUTH"}


@pytest.mark.skipif(
    os.environ.get("DEVFLOW_TEST_REAL_FETCH") != "1",
    reason="Real transport opt-in; Windows sandbox sh.exe signal pipe is blocked (WinError 5)",
)
def test_local_fetch_transport_integration(objects, monkeypatch):
    cache = objects[0]
    monkeypatch.setattr(cache.runner, "run", objects[6])
    assert fetch(objects).sha == objects[2]


def test_reused_attempt_cannot_change_pinned_ref(objects):
    attempt = uuid4().hex
    original = fetch(objects, attempt)
    with pytest.raises(api().WorkspaceProcessError, match="attempt_exists"):
        fetch(objects, attempt)
    assert (
        objects[0].root / "1.git" / original.pin_ref
    ).read_text().strip() == original.sha


@pytest.mark.parametrize(
    "name,ref",
    [
        ("../bad/project", "refs/heads/main"),
        ("evil.test/p", "-u"),
        ("team/project", "HEAD"),
        ("team/project", "refs/heads/x:refs/heads/main"),
    ],
)
def test_fetch_rejects_untrusted_identity_and_ref_before_spawn(objects, name, ref):
    cache, guard, *_ = objects
    with pytest.raises(api().WorkspaceProcessError, match="invalid_input"):
        cache.fetch(
            1,
            name,
            ref,
            SecretStr("secret"),
            uuid4().hex,
            guard=guard(),
            deadline=time.monotonic() + 5,
            cancel=threading.Event(),
        )


def test_wrong_object_type_and_blob_limit_fail_closed(objects):
    cache, guard, commit, blob, *_ = objects
    fetch(objects)
    with cache.operation(
        guard("read"), deadline=time.monotonic() + 5, cancel=threading.Event()
    ):
        with pytest.raises(api().WorkspaceProcessError, match="object_type"):
            cache.blob(1, commit, 1024)
        with pytest.raises(api().WorkspaceProcessError, match="blob_too_large"):
            cache.blob(1, blob, 2)
        with pytest.raises(api().WorkspaceProcessError, match="invalid_input"):
            cache.tree(1, "HEAD")


def test_service_cache_rejects_unexpected_config_and_alternates(objects):
    cache, guard, *_ = objects
    fetch(objects)
    repo = cache.root / "1.git"
    original = (repo / "config").read_bytes()
    (repo / "config").write_bytes(original + b"[diff]\n external = malicious-command\n")
    with (
        cache.operation(
            guard("read"), deadline=time.monotonic() + 5, cancel=threading.Event()
        ),
        pytest.raises(api().WorkspaceProcessError, match="cache_invalid"),
    ):
        cache.tree(1, objects[2])
    (repo / "config").write_bytes(original)
    (repo / "objects" / "info" / "alternates").write_text("/untrusted")
    with (
        cache.operation(
            guard("read"), deadline=time.monotonic() + 5, cancel=threading.Event()
        ),
        pytest.raises(api().WorkspaceProcessError, match="cache_invalid"),
    ):
        cache.tree(1, objects[2])


def test_annotated_tag_resolves_and_pins_its_commit(objects):
    cache, guard, commit, _, _, _, _, _, git = objects
    tag = git(
        "hash-object",
        "-t",
        "tag",
        "-w",
        "--stdin",
        data=(
            f"object {commit}\ntype commit\ntag v1\ntagger Fixture <fixture@example.invalid> 1 +0000\n\nrelease\n".encode()
        ),
    )
    git("update-ref", "refs/tags/v1", tag.decode())
    result = cache.fetch(
        1,
        "team/project",
        "refs/tags/v1",
        SecretStr("sentinel"),
        uuid4().hex,
        guard=guard(),
        deadline=time.monotonic() + 15,
        cancel=threading.Event(),
    )
    assert result.sha == commit
    assert (cache.root / "1.git" / result.pin_ref).read_text().strip() == commit


def test_missing_reachable_object_prevents_pin(objects):
    cache, _, _, _, _, _, _, _, git = objects
    tree = git(
        "mktree", "--missing", data=b"100644 blob " + b"e" * 40 + b"\tmissing.txt\n"
    )
    sha = git(
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit-tree",
        tree.decode(),
        "-m",
        "missing",
    )
    git("update-ref", "refs/heads/main", sha.decode())
    attempt = uuid4().hex
    with pytest.raises(api().WorkspaceProcessError, match="git_failed"):
        fetch(objects, attempt)
    assert not (cache.root / "1.git" / "refs/devflow/pins" / attempt).exists()


def test_invalid_utf8_tree_name_remains_raw_bytes(objects):
    cache, guard, _, blob, _, _, _, _, git = objects
    tree = git("mktree", data=b"100644 blob " + blob.encode() + b"\tinvalid-\xff\n")
    sha = git(
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit-tree",
        tree.decode(),
        "-m",
        "raw-name",
    )
    git("update-ref", "refs/heads/main", sha.decode())
    result = fetch(objects)
    with cache.operation(
        guard("read"), deadline=time.monotonic() + 5, cancel=threading.Event()
    ):
        entries = cache.tree(1, result.sha)
    assert entries.entries[0].path == b"invalid-\xff"


def test_fetch_bootstrap_never_mutates_cache_in_caller(objects, monkeypatch):
    cache = objects[0]
    repo = cache.root / "1.git"
    original_mkdir = Path.mkdir
    original_write = Path.write_bytes

    def reject_caller_mkdir(path, *args, **kwargs):
        if path == repo:
            pytest.fail("repository mkdir executed in application caller")
        return original_mkdir(path, *args, **kwargs)

    def reject_caller_config(path, *args, **kwargs):
        if path == repo / "config":
            pytest.fail("repository config write executed in application caller")
        return original_write(path, *args, **kwargs)

    # Fault injection applies only in this application process. The fixed
    # supervisor must carry out actual bootstrap under its owned OS guard.
    monkeypatch.setattr(Path, "mkdir", reject_caller_mkdir)
    monkeypatch.setattr(Path, "write_bytes", reject_caller_config)
    assert fetch(objects).sha == objects[2]
    assert (repo / "config").read_bytes() == git_api()._CONFIG


def test_long_raw_tree_path_reaches_consumer_classification(objects):
    cache, guard, _, blob, _, _, _, _, git = objects
    path = b"long-" + b"x" * 4096
    tree = git("mktree", data=b"100644 blob " + blob.encode() + b"\t" + path + b"\n")
    sha = git(
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit-tree",
        tree.decode(),
        "-m",
        "long-name",
    )
    git("update-ref", "refs/heads/main", sha.decode())
    result = fetch(objects)
    with cache.operation(
        guard("read"), deadline=time.monotonic() + 5, cancel=threading.Event()
    ):
        entries = cache.tree(1, result.sha)
    assert entries.complete and entries.scanned == 1
    assert entries.entries[0].path == path


@pytest.mark.parametrize("phase", ["mkdir", "config", "unknown"])
def test_bootstrap_filesystem_failure_preserves_only_known_drain_proof(
    objects, monkeypatch, phase
):
    cache, make_guard, *_ = objects
    mod = api()
    # Each failure owns its admission semaphore; the unknown-proof case must
    # retain it without poisoning other tests' unrelated operations.
    slot = threading.BoundedSemaphore(1)
    monkeypatch.setattr(mod, "_WRITERS", slot)
    entry = Path(cache.runner._supervisor_command()[1])
    injection = f"""
import devflow.workspace.process as process
phase = {phase!r}
original_mkdir = Path.mkdir
original_write = Path.write_bytes
original_run = process._run_tree
def fail_mkdir(path, *args, **kwargs):
    if phase == "mkdir" and path.name == "1.git":
        raise OSError("private filesystem detail")
    return original_mkdir(path, *args, **kwargs)
def fail_config(path, *args, **kwargs):
    if phase == "config" and path.name == "config":
        raise OSError("private filesystem detail")
    return original_write(path, *args, **kwargs)
def observed_run(*args):
    if phase == "unknown":
        return process.ProcessResult("cleanup_required"), False
    result, proven = original_run(*args)
    if proven:
        diagnostic_path.with_suffix(".proof").write_text("drained")
    return result, proven
Path.mkdir = fail_mkdir
Path.write_bytes = fail_config
process._run_tree = observed_run
"""
    entry.write_text(
        SUPERVISOR.replace(
            "_supervisor_main(activate=", injection + "\n_supervisor_main(activate="
        )
    )
    guard = make_guard()
    if phase == "unknown":
        with (
            pytest.raises(mod.WorkspaceProcessError, match="^cleanup_required$"),
            cache.operation(
                guard, deadline=time.monotonic() + 5, cancel=threading.Event()
            ),
        ):
            assert cache.runner.bootstrap().status == "cleanup_required"
    else:
        with cache.operation(
            guard, deadline=time.monotonic() + 5, cancel=threading.Event()
        ):
            result = cache.runner.bootstrap()
            assert result.status == "cache_invalid"
            assert "private filesystem detail" not in repr(result)
    state = json.loads(Path(guard.database_url).read_text())
    assert state["state"] == ("active" if phase == "unknown" else "drained")
    acquired = slot.acquire(blocking=False)
    assert acquired is (phase != "unknown")
    if acquired:
        slot.release()
    proof = Path(guard.database_url).with_suffix(".proof")
    assert proof.exists() is (phase == "config")
