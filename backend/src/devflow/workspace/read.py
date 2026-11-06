"""Exact immutable evidence, bounded by one authorization and physical operation."""

import asyncio
import hashlib
import json
import logging
import re
import threading
import time
import unicodedata
from collections import Counter
from dataclasses import dataclass
from urllib.parse import quote
from uuid import UUID

from sqlalchemy import func, select, update

from devflow.errors import AppError
from devflow.pagination import CursorCodec
from devflow.security import read_secret_file
from devflow.workspace.guards import reserve_guard, revoke_reservation
from devflow.workspace.handlers import owned_thread
from devflow.workspace.models import WorkspaceVersion
from devflow.workspace.process import WorkspaceProcessError
from devflow.workspace.schemas import (
    CodeCitation,
    FileView,
    SearchHit,
    SearchScan,
    SearchView,
    TreeItem,
    TreePage,
)
from devflow.workspace.scope import capture, denied, lock_scopes
from devflow.workspace.service import WorkspaceService

BLOB_BYTES = 1024 * 1024
TEXT_BYTES = 256 * 1024
SEARCH_BYTES = 20 * 1024 * 1024
MODES = {"100644": "file", "100755": "file", "120000": "symlink", "160000": "gitlink"}
logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SourceMetadata:
    path: str | None
    mode: str
    oid: str
    size: int | None


@dataclass(frozen=True, slots=True)
class MetadataPage:
    items: tuple[SourceMetadata, ...]
    next_cursor: int | None
    exhausted: bool
    truncated: bool


class RawSourceError(AppError):
    """Finite public error with a stable internal per-item exclusion reason."""

    def __init__(self, reason):
        super().__init__("validation_error", 422, "Unsupported source")
        self.reason = reason


def invalid():
    return AppError("validation_error", 422, "Invalid workspace read")


def valid_path(value, *, root=False):
    try:
        if not isinstance(value, str) or len(value.encode("utf-8")) > 4096:
            return False
    except UnicodeError:
        return False
    return (root and value == "") or (
        bool(value)
        and "\\" not in value
        and not any(unicodedata.category(char) == "Cc" for char in value)
        and all(part not in {"", ".", ".."} for part in value.split("/"))
    )


def decoded_path(raw):
    try:
        value = raw.decode("utf-8")
    except UnicodeError:
        return None
    return value if valid_path(value) else None


def lines(raw):
    # splitlines() would also split CR and Unicode separators, changing evidence.
    pieces = raw.split(b"\n")
    return [piece + b"\n" for piece in pieces[:-1]] + (
        [pieces[-1]] if pieces[-1] else []
    )


def text_kind(raw):
    if b"\0" in raw:
        return "binary"
    try:
        raw.decode("utf-8")
    except UnicodeError:
        return "non_utf8"
    if len(raw) <= 1024 and re.fullmatch(
        rb"version https://git-lfs.github.com/spec/v1\r?\n"
        rb"(?:ext-[^\r\n]+\r?\n)*"
        rb"oid sha256:[0-9a-f]{64}\r?\nsize [0-9]+\r?\n?",
        raw,
    ):
        return "lfs_pointer"
    return "text"


def source_url(version, path, start=None, end=None, *, directory=False):
    name = next(
        scope["full_name"]
        for scope in version.scopes
        if scope["repository_id"] == str(version.source_repository_id)
    )
    encoded = "/".join(quote(part, safe="") for part in path.split("/"))
    url = (
        "https://github.com/"
        + "/".join(quote(part, safe="") for part in name.split("/"))
        + ("/tree/" if directory else "/blob/")
        + version.commit_sha
        + "/"
        + encoded
    )
    if start is not None:
        url += f"#L{start}" + (f"-L{end}" if end != start else "")
    return url


def scope_fields(version, path):
    return {
        "repository_id": version.repository_id,
        "workspace_id": version.id,
        "source_repository_id": version.source_repository_id,
        "commit_sha": version.commit_sha,
        "path": path,
    }


def check_time(deadline, cancel):
    if cancel.is_set():
        raise WorkspaceProcessError("cancelled")
    if time.monotonic() >= deadline:
        raise WorkspaceProcessError("timeout")


class WorkspaceReader:
    def __init__(self, factory, policy, cache):
        self.factory, self.policy, self.cache = factory, policy, cache
        self.settings = policy.settings

    async def tree(self, actor, repo, workspace_id, path="", cursor=None, limit=200):
        return await self._read(
            actor, repo, workspace_id, "tree", path, cursor=cursor, limit=limit
        )

    async def file(self, actor, repo, workspace_id, path, start_line=1, line_count=500):
        return await self._read(
            actor,
            repo,
            workspace_id,
            "file",
            path,
            start_line=start_line,
            line_count=line_count,
        )

    async def search(self, actor, repo, workspace_id, query, path=""):
        return await self._read(actor, repo, workspace_id, "search", path, query=query)

    async def raw(self, actor, repo, workspace_id, path, *, sha, expected_oid=None):
        """Complete allowed text, materialized under one existing read guard."""
        return await self._read(
            actor,
            repo,
            workspace_id,
            "raw",
            path,
            expected_sha=sha,
            expected_oid=expected_oid,
        )

    async def metadata(self, actor, repo, workspace_id, *, sha, cursor=0, limit=16):
        """Internal fixed-SHA metadata page; public callers must bind their cursor."""
        if (
            type(cursor) is not int
            or not 0 <= cursor <= 100000
            or type(limit) is not int
            or not 1 <= limit <= 100
        ):
            raise invalid()
        return await self._read(
            actor,
            repo,
            workspace_id,
            "metadata",
            "",
            expected_sha=sha,
            ordinal=cursor,
            limit=limit,
        )

    def _metadata(self, version, path, entries, deadline, cancel, *, ordinal, limit):
        ordered = sorted(entries.entries, key=lambda item: item.path)
        selected = ordered[ordinal : ordinal + limit]
        items = []
        for entry in selected:
            check_time(deadline, cancel)
            size = (
                self.cache.blob_size(version.cache_repository_id, entry.oid)
                if MODES[entry.mode] == "file"
                else None
            )
            items.append(
                SourceMetadata(decoded_path(entry.path), entry.mode, entry.oid, size)
            )
        next_position = ordinal + len(selected)
        more = next_position < len(ordered)
        return MetadataPage(
            tuple(items),
            next_position if more else None,
            not more and entries.complete and not entries.truncated,
            not entries.complete or entries.truncated,
        )

    async def _local_fence(self, actor, version):
        # Base denial keeps repository semantics; a stale but still authorized
        # snapshot or a foreign denial has its own local-code error.
        ordered = sorted(
            version.scopes,
            key=lambda scope: scope["repository_id"] != str(version.repository_id),
        )
        for expected in ordered:
            foreign = expected["repository_id"] != str(version.repository_id)
            try:
                _, key = await self.policy.local(actor, UUID(expected["repository_id"]))
            except AppError as error:
                if foreign and error.status in {403, 404}:
                    raise denied("fork_source_unavailable") from None
                raise
            if (key[3], key[4], key[5]) != (
                expected["installation_version"],
                expected["repository_version"],
                expected["installation_id"],
            ):
                raise denied(
                    "fork_source_unavailable" if foreign else "workspace_not_found"
                )

    async def _fence(self, actor, version):
        await self._local_fence(actor, version)
        try:
            async with self.factory.begin() as db:
                # One ordered fence closes a base-change/source-check race;
                # independent local() snapshots alone cannot cover both scopes.
                await lock_scopes(db, version.scopes, self.settings, actor=actor)
        except AppError as error:
            if error.code != "permission_revoked":
                raise
            await self._local_fence(actor, version)
            raise denied("workspace_not_found") from None

    async def _read(self, actor, repo, identity, kind, path, **options):
        deadline = time.monotonic() + 5
        cancel, guard, version = threading.Event(), None, None
        try:
            async with asyncio.timeout(max(0, deadline - time.monotonic())):
                base = await capture(self.policy, actor, repo, fresh=False)
                async with self.factory() as db:
                    version = await db.scalar(
                        select(WorkspaceVersion).where(
                            WorkspaceVersion.id == identity,
                            WorkspaceVersion.repository_id == repo,
                        )
                    )
                if version is None:
                    raise denied("workspace_not_found")
                service = WorkspaceService(self.factory, self.policy, self.settings)
                await service._authorize_version(actor, version, base, fresh=False)
                expected_sha = options.pop("expected_sha", None)
                if expected_sha is not None and version.commit_sha != expected_sha:
                    raise AppError("version_changed", 409, "Source version changed")
                if not valid_path(path, root=kind not in {"file", "raw"}):
                    raise invalid()
                if kind == "tree":
                    if (
                        type(options["limit"]) is not int
                        or not 1 <= options["limit"] <= 200
                    ):
                        raise invalid()
                    codec = CursorCodec(
                        read_secret_file(self.settings.app_signing_key_file)
                    )
                    scope = json.dumps(
                        [str(repo), str(identity), version.commit_sha, path],
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ).encode("utf-8")
                    purpose = "workspace-tree:v1:" + hashlib.sha256(scope).hexdigest()
                    ordinal = 0
                    if options["cursor"] is not None:
                        position = codec.decode_cursor(
                            options["cursor"],
                            actor.id,
                            purpose,
                            options["limit"],
                            max_limit=200,
                        )
                        ordinal = position.get("ordinal")
                        if (
                            set(position) != {"ordinal"}
                            or type(ordinal) is not int
                            or not 0 <= ordinal <= 100000
                        ):
                            raise AppError("invalid_cursor", 400, "Invalid tree cursor")
                    options.update(
                        codec=codec, purpose=purpose, ordinal=ordinal, actor=actor.id
                    )
                elif kind == "file":
                    if (
                        type(options["start_line"]) is not int
                        or options["start_line"] < 1
                        or type(options["line_count"]) is not int
                        or not 1 <= options["line_count"] <= 500
                    ):
                        raise invalid()
                elif kind == "search":
                    query = options["query"]
                    try:
                        valid = (
                            isinstance(query, str)
                            and "\n" not in query
                            and 1 <= len(query.encode("utf-8")) <= 256
                        )
                    except UnicodeError:
                        valid = False
                    if not valid:
                        raise invalid()
                guard = await reserve_guard(
                    self.factory,
                    self.settings,
                    identity,
                    kind="read",
                    actor=actor,
                    deadline=deadline,
                    lock_path=self.cache.lock_path(version.cache_repository_id),
                )

                def materialize():
                    with self.cache.operation(guard, deadline=deadline, cancel=cancel):
                        check_time(deadline, cancel)
                        entries = self.cache.tree(
                            version.cache_repository_id, version.commit_sha
                        )
                        check_time(deadline, cancel)
                        return getattr(self, "_" + kind)(
                            version, path, entries, deadline, cancel, **options
                        )

                result = await owned_thread(materialize, cancel)
                await self._fence(actor, version)
                async with self.factory.begin() as db:
                    await db.execute(
                        update(WorkspaceVersion)
                        .where(WorkspaceVersion.id == identity)
                        .values(last_used_at=func.clock_timestamp())
                    )
                await self._fence(actor, version)
                return result
        except TimeoutError:
            raise AppError("attempt_timeout", 503, "Workspace read timed out") from None
        except WorkspaceProcessError as error:
            code = {
                "timeout": "attempt_timeout",
                "cleanup_required": "cleanup_required",
            }.get(str(error), "git_failed")
            raise AppError(code, 503, "Workspace read unavailable") from None
        except AppError as error:
            if error.code == "permission_revoked" and version is not None:
                # Reservation's local fence can observe a race after initial auth.
                remaining = deadline - time.monotonic()
                try:
                    if remaining <= 0:
                        raise TimeoutError
                    async with asyncio.timeout(remaining):
                        await self._fence(actor, version)
                except TimeoutError:
                    raise AppError(
                        "attempt_timeout", 503, "Workspace read timed out"
                    ) from None
                raise denied("workspace_not_found") from None
            raise
        finally:
            cancel.set()
            if guard is not None:
                # Only a never-activated nonce may be revoked by the API. Real
                # physical drain is the supervisor's responsibility.
                async def revoke():
                    remaining = deadline + 2 - time.monotonic()
                    if remaining > 0:
                        try:
                            async with asyncio.timeout(remaining):
                                await revoke_reservation(self.factory, guard)
                        except Exception:  # noqa: BLE001 - retain durable exclusion
                            logger.warning(
                                "Workspace read reservation cleanup unavailable"
                            )

                cleanup = asyncio.create_task(revoke())
                interrupted = False
                while not cleanup.done():
                    try:
                        await asyncio.shield(cleanup)
                    except asyncio.CancelledError:
                        interrupted = True
                if interrupted:
                    raise asyncio.CancelledError

    @staticmethod
    def _directory(path, entries):
        if not path:
            return
        prefix = path.encode("utf-8") + b"/"
        if any(entry.path.startswith(prefix) for entry in entries.entries):
            return
        if not entries.complete:
            return
        raise denied("workspace_not_found")

    def _raw(self, version, path, entries, deadline, cancel, *, expected_oid=None):
        if expected_oid is not None and not re.fullmatch(r"[0-9a-f]{40}", expected_oid):
            raise invalid()
        if expected_oid is None and (not entries.complete or entries.truncated):
            raise AppError("service_unavailable", 503, "Workspace tree unavailable")
        entry = next(
            (item for item in entries.entries if item.path == path.encode("utf-8")),
            None,
        )
        if entry is None:
            raise denied("workspace_not_found")
        if expected_oid is not None and entry.oid != expected_oid:
            raise AppError("version_changed", 409, "Source object changed")
        if MODES[entry.mode] != "file":
            raise RawSourceError(MODES[entry.mode])
        check_time(deadline, cancel)
        try:
            raw = self.cache.blob(version.cache_repository_id, entry.oid, BLOB_BYTES)
        except WorkspaceProcessError as error:
            if str(error) != "blob_too_large":
                raise
            raise RawSourceError("too_large") from None
        kind = text_kind(raw)
        if kind != "text":
            raise RawSourceError(kind)
        # One bounded materialization, segmented inside the same physical operation.
        parts = []
        for offset in range(0, len(raw), 65536):
            check_time(deadline, cancel)
            parts.append(raw[offset : offset + 65536])
        check_time(deadline, cancel)
        return b"".join(parts)

    def _tree(
        self,
        version,
        path,
        entries,
        deadline,
        cancel,
        *,
        ordinal,
        limit,
        codec,
        purpose,
        actor,
        cursor,
    ):
        self._directory(path, entries)
        prefix = path.encode("utf-8") + b"/" if path else b""
        items, seen, skipped = [], set(), Counter()
        for entry in entries.entries:
            check_time(deadline, cancel)
            if not entry.path.startswith(prefix):
                continue
            tail = entry.path[len(prefix) :]
            part = tail.split(b"/", 1)[0]
            raw = prefix + part
            if raw in seen:
                continue
            seen.add(raw)
            directory = b"/" in tail
            supported = decoded_path(raw)
            if supported is None:
                skipped["unsupported_path"] += 1
            items.append(
                TreeItem(
                    path=supported,
                    display_name=part.decode("utf-8")
                    if supported
                    else repr(part[:128])[2:-1][:256],
                    kind="unsupported"
                    if supported is None
                    else "directory"
                    if directory
                    else MODES[entry.mode],
                    mode="040000" if directory else entry.mode,
                    oid=None if directory else entry.oid,
                    reason="unsupported_path" if supported is None else None,
                    source_url=source_url(version, supported, directory=directory)
                    if supported
                    else None,
                )
            )
        if ordinal > len(items):
            raise AppError("invalid_cursor", 400, "Invalid tree cursor")
        page = items[ordinal : ordinal + limit]
        following = ordinal + len(page)
        more = following < len(items)
        next_cursor = (
            codec.encode_cursor(
                actor, purpose, {"ordinal": following}, limit, max_limit=200
            )
            if more
            else None
        )
        return TreePage(
            **scope_fields(version, path),
            items=page,
            next_cursor=next_cursor,
            has_more=more,
            complete=entries.complete,
            truncated=entries.truncated,
            reason=entries.reason,
            scanned=entries.scanned,
            skipped=dict(skipped),
        )

    def _file(
        self, version, path, entries, deadline, cancel, *, start_line, line_count
    ):
        data = {
            **scope_fields(version, path),
            "blob_oid": None,
            "scanned": entries.scanned,
            "requested_start_line": start_line,
            "requested_line_count": line_count,
            "skipped": {},
        }
        if entries.truncated:
            return FileView(
                **data, kind="unavailable", truncated=True, reason="tree_limit"
            )
        entry = next(
            (item for item in entries.entries if item.path == path.encode("utf-8")),
            None,
        )
        if entry is None:
            raise denied("workspace_not_found")
        kind = MODES[entry.mode]
        data["blob_oid"] = None if kind == "gitlink" else entry.oid
        if kind != "file":
            return FileView(**data, kind=kind, reason=kind)
        check_time(deadline, cancel)
        try:
            raw = self.cache.blob(version.cache_repository_id, entry.oid, BLOB_BYTES)
        except WorkspaceProcessError as error:
            if str(error) != "blob_too_large":
                raise
            return FileView(**data, kind="too_large", reason="too_large")
        data["size_bytes"] = len(raw)
        kind = text_kind(raw)
        if kind != "text":
            return FileView(**data, kind=kind, reason=kind)
        all_lines = lines(raw)
        chosen, size = [], 0
        for line in all_lines[start_line - 1 : start_line - 1 + line_count]:
            check_time(deadline, cancel)
            if size + len(line) > TEXT_BYTES:
                break
            chosen.append(line)
            size += len(line)
        actual_end = start_line + len(chosen) - 1 if chosen else None
        more = start_line - 1 + len(chosen) < len(all_lines)
        byte_limited = more and len(chosen) < line_count
        complete = start_line == 1 and len(chosen) == len(all_lines)
        return FileView(
            **data,
            kind="text",
            text=b"".join(chosen).decode("utf-8"),
            total_lines=len(all_lines),
            start_line=start_line if chosen else None,
            end_line=actual_end,
            next_line=start_line + len(chosen) if more else None,
            source_url=source_url(version, path, start_line, actual_end)
            if chosen
            else None,
            complete=complete,
            truncated=not complete,
            reason="response_limit"
            if byte_limited
            else "line_limit"
            if not complete
            else None,
        )

    def _search(self, version, path, entries, deadline, cancel, *, query):
        self._directory(path, entries)
        scanned = {
            "tree_entries": entries.scanned,
            "candidates": 0,
            "text_files": 0,
            "blob_bytes": 0,
        }
        items, skipped, reason, snippet_bytes = [], Counter(), entries.reason, 0
        snippets_truncated = False
        prefix = path.encode("utf-8") + b"/" if path else b""
        if entries.complete:
            for entry in entries.entries:
                check_time(deadline, cancel)
                if not entry.path.startswith(prefix):
                    continue
                # Stop before unbounded extra reads or a 101st result.
                if (
                    len(items) == 100
                    or scanned["text_files"] == 2000
                    or scanned["blob_bytes"] == SEARCH_BYTES
                ):
                    reason = (
                        "match_limit"
                        if len(items) == 100
                        else "file_limit"
                        if scanned["text_files"] == 2000
                        else "byte_limit"
                    )
                    break
                scanned["candidates"] += 1
                name = decoded_path(entry.path)
                kind = MODES[entry.mode]
                if name is None or kind != "file":
                    skipped["unsupported_path" if name is None else kind] += 1
                    continue
                maximum = min(BLOB_BYTES, SEARCH_BYTES - scanned["blob_bytes"])
                try:
                    raw = self.cache.blob(
                        version.cache_repository_id, entry.oid, maximum
                    )
                except WorkspaceProcessError as error:
                    if str(error) != "blob_too_large":
                        raise
                    if maximum < BLOB_BYTES:
                        reason = "byte_limit"
                        break
                    skipped["too_large"] += 1
                    continue
                scanned["blob_bytes"] += len(raw)
                kind = text_kind(raw)
                if kind != "text":
                    skipped[kind] += 1
                    continue
                scanned["text_files"] += 1
                all_lines = lines(raw)
                for number, line in enumerate(all_lines, 1):
                    check_time(deadline, cancel)
                    if len(items) == 100:
                        reason = "match_limit"
                        break
                    # LF is a separator, not searchable line content. CR stays.
                    if query not in line.removesuffix(b"\n").decode("utf-8"):
                        continue
                    fits = snippet_bytes + len(line) <= TEXT_BYTES
                    text = line.decode("utf-8") if fits else None
                    if fits:
                        snippet_bytes += len(line)
                    else:
                        snippets_truncated = True
                    items.append(
                        SearchHit(
                            citation=CodeCitation(
                                **scope_fields(version, name),
                                blob_oid=entry.oid,
                                start_line=number,
                                end_line=number,
                                source_url=source_url(version, name, number, number),
                            ),
                            text=text,
                            reason=None if fits else "response_limit",
                        )
                    )
                if reason is not None:
                    break
        return SearchView(
            **scope_fields(version, path),
            query=query,
            items=items,
            complete=reason is None,
            truncated=reason is not None,
            reason=reason,
            scanned=SearchScan(**scanned),
            skipped=dict(skipped),
            snippets_truncated=snippets_truncated,
        )
