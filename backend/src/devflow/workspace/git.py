"""Fixed GitHub fetch and raw-object operations over service-owned bare caches."""

import base64
import re
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from pydantic import SecretStr

from .process import MAX_OUTPUT, WorkspaceProcessError

_CONFIG = b"[core]\n\trepositoryformatversion = 0\n\tbare = true\n"
_OID = re.compile(r"[0-9a-f]{40}\Z")  # GitHub SHA-1 repositories, no abbreviation
_ATTEMPT = re.compile(r"[0-9a-f]{32}\Z")
_MODES = {"100644": "blob", "100755": "blob", "120000": "blob", "160000": "commit"}


@dataclass(frozen=True)
class GitCommit:
    sha: str
    tree_oid: str
    pin_ref: str


@dataclass(frozen=True)
class TreeEntry:
    mode: str
    oid: str
    path: bytes


@dataclass(frozen=True)
class TreeEntries:
    entries: tuple[TreeEntry, ...]
    complete: bool
    truncated: bool
    scanned: int
    reason: str | None


def valid_full_name(value):
    if not isinstance(value, str) or len(value) > 140:
        return False
    parts = value.split("/")
    return (
        len(parts) == 2
        and bool(
            re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?", parts[0])
        )
        and bool(re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", parts[1]))
        and parts[1] not in {".", ".."}
    )


def valid_ref(value):
    if not isinstance(value, str) or not value.startswith(
        ("refs/heads/", "refs/tags/", "refs/pull/")
    ):
        return False
    if len(value.encode()) > 1024 or any(c in value for c in ("..", "@{", "//")):
        return False
    if any(ord(c) < 33 or ord(c) == 127 or c in "~^:?*[\\" for c in value):
        return False
    return all(
        part and not part.startswith(".") and not part.endswith((".", ".lock"))
        for part in value.split("/")
    )


def _oid(value):
    if not isinstance(value, str) or not _OID.fullmatch(value):
        raise WorkspaceProcessError("invalid_input")
    return value


def _repository_id(value):
    if type(value) is not int or not 0 < value < 2**63:
        raise WorkspaceProcessError("invalid_input")
    return value


class GitCache:
    def __init__(self, root, runner):
        self.root = Path(root).resolve()
        self.runner = runner

    def lock_path(self, repository_id):
        return self.root / ".locks" / f"{_repository_id(repository_id)}.lock"

    def prepare_lock(self, repository_id):
        """Worker-only initialization; API reads never create a missing guard."""
        path = self.lock_path(repository_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with path.open("xb"):
                pass
        except FileExistsError:
            if path.is_symlink() or not path.is_file():
                raise WorkspaceProcessError("cache_invalid") from None
        return path

    @contextmanager
    def operation(self, guard, *, deadline, cancel):
        if guard.lock_path != self.lock_path(guard.repository_id):
            raise WorkspaceProcessError("invalid_input")
        with self.runner.operation(
            guard, deadline=deadline, cancel=cancel
        ) as operation:
            yield operation

    def _repo(self, repository_id, *, existing=True):
        repository_id = _repository_id(repository_id)
        if self.runner.current.guard.repository_id != repository_id:
            raise WorkspaceProcessError("guard_scope")
        repo = self.root / f"{repository_id}.git"
        if repo.is_symlink():
            raise WorkspaceProcessError("cache_invalid")
        if existing:
            try:
                if (
                    (repo / "config").is_symlink()
                    or (repo / "config").read_bytes() != _CONFIG
                    or not (repo / "objects").is_dir()
                    or (repo / "objects").is_symlink()
                    or (repo / "objects" / "info" / "alternates").exists()
                    or (repo / "objects" / "info" / "http-alternates").exists()
                ):
                    raise WorkspaceProcessError("cache_invalid")
            except OSError:
                raise WorkspaceProcessError("cache_unavailable") from None
        return repo

    def _run(
        self, args, repo, *, maximum=4096, env=None, record_limit=None, allowed=()
    ):
        operation = self.runner.current
        remaining = operation.deadline - time.monotonic()
        if remaining <= 0:
            raise WorkspaceProcessError("timeout")
        result = self.runner.run(
            args,
            cwd=repo,
            env=env or {},
            timeout=remaining,
            max_output=maximum,
            cancel=operation.cancel,
            record_limit=record_limit,
        )
        if result.status != "ok" and result.status not in allowed:
            raise WorkspaceProcessError(result.status)
        return result

    def _type(self, repo, oid, expected):
        result = self._run(["cat-file", "-t", _oid(oid)], repo)
        if result.stdout.strip() != expected.encode():
            raise WorkspaceProcessError("object_type")

    def _commit(self, repo, oid):
        """Peel at most eight annotated tags, retaining full object/type checks."""
        visited = set()
        for depth in range(9):
            kind = self._run(["cat-file", "-t", _oid(oid)], repo).stdout.strip()
            if kind == b"commit":
                return oid
            if kind != b"tag" or depth == 8 or oid in visited:
                raise WorkspaceProcessError("object_type")
            visited.add(oid)
            tag = self._run(["cat-file", "tag", oid], repo, maximum=1024 * 1024).stdout
            first = tag.split(b"\n", 1)[0]
            if not re.fullmatch(rb"object [0-9a-f]{40}", first):
                raise WorkspaceProcessError("object_invalid")
            oid = first[7:].decode("ascii")
        raise WorkspaceProcessError("object_type")

    def fetch(
        self,
        repository_id: int,
        full_name: str,
        ref: str,
        token: SecretStr,
        attempt_key: str,
        *,
        guard,
        deadline: float,
        cancel: threading.Event,
        budget_check=None,
    ) -> GitCommit:
        """Fetch one explicit ref; pin once. Lease/auth publication belongs to caller."""
        _repository_id(repository_id)
        if (
            not valid_full_name(full_name)
            or not valid_ref(ref)
            or not isinstance(attempt_key, str)
            or not _ATTEMPT.fullmatch(attempt_key)
            or not isinstance(token, SecretStr)
            or not token.get_secret_value()
            or guard.kind != "write"
            or guard.repository_id != repository_id
        ):
            raise WorkspaceProcessError("invalid_input")
        with self.operation(guard, deadline=deadline, cancel=cancel):
            bootstrap = self.runner.bootstrap()
            if bootstrap.status != "ok":
                raise WorkspaceProcessError(bootstrap.status)
            repo = self._repo(repository_id)
            version = self._run(["--version"], repo).stdout
            match = re.match(rb"git version (\d+)\.(\d+)", version)
            if not match or (int(match[1]), int(match[2])) < (2, 43):
                raise WorkspaceProcessError("git_unsupported")
            attempt_ref = f"refs/devflow/attempts/{attempt_key}"
            pin_ref = f"refs/devflow/pins/{attempt_key}"
            for candidate in (pin_ref, attempt_ref):
                result = self._run(
                    ["show-ref", "--verify", "--quiet", candidate],
                    repo,
                    allowed=("git_failed",),
                )
                if result.status == "ok":
                    raise WorkspaceProcessError("attempt_exists")
                if result.returncode != 1:
                    raise WorkspaceProcessError("cache_invalid")
            if budget_check:
                budget_check(repo)
            encoded = base64.b64encode(
                ("x-access-token:" + token.get_secret_value()).encode()
            ).decode()
            self._run(
                [
                    "fetch",
                    "--depth=1",
                    "--no-tags",
                    "--no-write-fetch-head",
                    "--no-recurse-submodules",
                    "--no-auto-maintenance",
                    "--force",
                    f"https://github.com/{full_name}.git",
                    f"+{ref}:{attempt_ref}",
                ],
                repo,
                env={"DEVFLOW_GIT_AUTH": "Authorization: Basic " + encoded},
            )
            # Git wrote this service-owned loose attempt ref under our exclusive lock.
            try:
                sha = _oid((repo / attempt_ref).read_text("ascii").strip())
            except (OSError, UnicodeError):
                raise WorkspaceProcessError("object_invalid") from None
            sha = self._commit(repo, sha)
            commit = self._run(
                ["cat-file", "commit", sha], repo, maximum=1024 * 1024
            ).stdout
            first = commit.split(b"\n", 1)[0]
            if not re.fullmatch(rb"tree [0-9a-f]{40}", first):
                raise WorkspaceProcessError("object_invalid")
            tree_oid = first[5:].decode("ascii")
            self._type(repo, tree_oid, "tree")
            self._run(
                ["fsck", "--connectivity-only", "--no-dangling", sha],
                repo,
                maximum=65536,
            )
            if budget_check:
                budget_check(repo)
            self._run(["update-ref", "--no-deref", pin_ref, sha, "0" * 40], repo)
            return GitCommit(sha, tree_oid, pin_ref)

    def tree(self, repo_id, sha) -> TreeEntries:
        _oid(sha)
        repo = self._repo(repo_id)
        self._type(repo, sha, "commit")
        result = self._run(
            ["ls-tree", "-r", "-z", "--full-tree", sha],
            repo,
            maximum=MAX_OUTPUT,
            record_limit=100000,
            allowed=("output_limit",),
        )
        entries = []
        records = result.stdout.split(b"\0")
        complete = result.status == "ok"
        if records[-1] and complete:
            raise WorkspaceProcessError("object_invalid")
        for record in records[:-1]:
            header, separator, path = record.partition(b"\t")
            fields = header.split(b" ")
            if len(fields) != 3 or not separator or not path:
                raise WorkspaceProcessError("object_invalid")
            try:
                mode, kind, oid = (field.decode("ascii") for field in fields)
                _oid(oid)
            except (UnicodeError, WorkspaceProcessError):
                raise WorkspaceProcessError("object_invalid") from None
            if (
                _MODES.get(mode) != kind
                or path.startswith(b"/")
                or any(part in {b"", b".", b".."} for part in path.split(b"/"))
            ):
                raise WorkspaceProcessError("object_invalid")
            entries.append(TreeEntry(mode, oid, path))
        return TreeEntries(
            tuple(entries),
            complete,
            not complete,
            len(entries),
            None if complete else "tree_limit",
        )

    def blob_size(self, repo_id, oid) -> int:
        _oid(oid)
        repo = self._repo(repo_id)
        self._type(repo, oid, "blob")
        raw_size = self._run(["cat-file", "-s", oid], repo).stdout.strip()
        if not raw_size.isdigit():
            raise WorkspaceProcessError("object_invalid")
        return int(raw_size)

    def blob(self, repo_id, oid, max_bytes) -> bytes:
        if type(max_bytes) is not int or not 0 < max_bytes <= 1024 * 1024:
            raise WorkspaceProcessError("invalid_input")
        size = self.blob_size(repo_id, oid)
        repo = self._repo(repo_id)
        if size > max_bytes:
            raise WorkspaceProcessError("blob_too_large")
        result = self._run(["cat-file", "blob", oid], repo, maximum=max(1, max_bytes))
        if len(result.stdout) != size:
            raise WorkspaceProcessError("object_invalid")
        return result.stdout

    def _ref(self, repo, ref):
        if not re.fullmatch(r"refs/devflow/(?:pins|attempts)/[0-9a-f]{32}", ref):
            raise WorkspaceProcessError("invalid_input")
        found = self._run(
            ["show-ref", "--verify", "--quiet", ref], repo, allowed=("git_failed",)
        )
        if found.status != "ok":
            if found.returncode == 1:
                return None
            raise WorkspaceProcessError("cache_invalid")
        output = self._run(["show-ref", "--verify", "--hash", ref], repo).stdout.strip()
        return _oid(output.decode("ascii"))

    def inspect(self, repo_id, pin, sha, tree):
        """Only local pin/commit/root-tree identity, never full blob completeness."""
        candidate = self.root / f"{_repository_id(repo_id)}.git"
        if not candidate.exists() and not candidate.is_symlink():
            return "unavailable"
        repo = self._repo(repo_id)
        for oid, kind in ((sha, b"commit"), (tree, b"tree")):
            result = self._run(
                ["cat-file", "-t", _oid(oid)], repo, allowed=("git_failed",)
            )
            if result.status != "ok":
                # A finite missing marker is needed; generic exit failure alone
                # is indeterminate. This bounded fallback is never used on success.
                listing = self._run(
                    ["cat-file", "--batch-all-objects", "--batch-check=%(objectname)"],
                    repo,
                    maximum=1024 * 1024,
                    allowed=("output_limit", "git_failed"),
                )
                if listing.status == "ok":
                    names = listing.stdout.splitlines()
                    if (
                        all(_OID.fullmatch(name.decode("ascii")) for name in names)
                        and oid.encode() not in names
                    ):
                        return "unavailable"
                return "unknown"
            if result.stdout.strip() != kind:
                return "unavailable"
        # show-ref rejects a dangling ref with a generic fatal exit. Establish
        # positive object absence first so that failure does not hide the proof.
        return "available" if self._ref(repo, pin) == sha else "unavailable"

    def remove_attempt(self, repo_id, operation_id, expected=None):
        if self.runner.current.guard.kind != "gc" or not re.fullmatch(
            r"[0-9a-f]{32}", operation_id
        ):
            raise WorkspaceProcessError("invalid_input")
        repo = self._repo(repo_id)
        for group in ("pins", "attempts"):
            ref = f"refs/devflow/{group}/{operation_id}"
            actual = self._ref(repo, ref)
            if actual is not None:
                if expected is not None and actual != expected:
                    raise WorkspaceProcessError("object_invalid")
                self._run(["update-ref", "--no-deref", "-d", ref, actual], repo)

    def collect(self, repo_id):
        if self.runner.current.guard.kind != "gc":
            raise WorkspaceProcessError("invalid_input")
        repo = self._repo(repo_id)
        self._run(["repack", "-Ad"], repo, maximum=65536)
        self._run(["prune", "--expire=now"], repo, maximum=65536)
