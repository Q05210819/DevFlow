"""Acceptance fixtures: real application state, synthetic GitHub HTTP only."""

import hashlib
import json
import os
import shutil
import zlib
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import httpx
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from devflow.config import Settings

ADMIN_ID = 41001
MEMBER_ID = 42002
INSTALLATION_ID = 11001
REPOSITORY_ID = 101001
REPOSITORY_NAME = "acceptance/private-repository"


def synthetic_settings(directory: Path, database_url: str, origin: str) -> Settings:
    """Use ordinary settings with ephemeral, explicitly synthetic secrets."""
    directory.mkdir(parents=True, exist_ok=True)
    secrets = {
        "github_client_secret_file": b"synthetic-acceptance-client-secret",
        "github_webhook_secret_file": b"synthetic-acceptance-webhook-secret",
        "app_signing_key_file": b"synthetic-acceptance-signing-key-32-bytes",
        "credential_encryption_key_file": Fernet.generate_key(),
        "github_private_key_file": rsa.generate_private_key(
            public_exponent=65537, key_size=2048
        ).private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ),
    }
    paths = {}
    for name, value in secrets.items():
        paths[name] = directory / name
        paths[name].write_bytes(value)
    return Settings(
        _env_file=None,
        app_env="test",
        public_base_url=origin,
        database_url=database_url,
        bootstrap_admin_github_id=ADMIN_ID,
        github_app_id=7001,
        github_client_id="synthetic-acceptance-client",
        github_allowed_installation_ids=[INSTALLATION_ID],
        workspace_cache_root=directory.resolve() / "workspace",
        **paths,
    )


class SyntheticGitHub:
    """Codes select a synthetic identity; production OAuth still validates state."""

    def __init__(self, workspace=None):
        self.observed = datetime.now(UTC).replace(microsecond=0) - timedelta(minutes=1)
        self.rate_limit_page2 = False
        self.actions_permission_missing = False
        self.workspace = workspace
        self.issued = {}
        self.workspace_requests = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.url.host == "github.com" and path == "/login/oauth/access_token":
            form = parse_qs(request.content.decode())
            code = form.get("code", [""])[0]
            if code not in {"acceptance-admin", "acceptance-member"}:
                return httpx.Response(400, json={"error": "bad_verification_code"})
            return httpx.Response(
                200,
                json={
                    "access_token": f"synthetic-{code}",
                    "refresh_token": f"synthetic-refresh-{code}",
                    "expires_in": 3600,
                    "refresh_token_expires_in": 7200,
                    "token_type": "bearer",
                },
            )
        assert request.url.host == "api.github.com"
        if path == f"/app/installations/{INSTALLATION_ID}/access_tokens":
            assert request.method == "POST"
            scope = json.loads(request.content)
            assert scope["repository_ids"] == [REPOSITORY_ID]
            allowed = [
                {"metadata": "read", key: "read"}
                for key in ("issues", "pull_requests", "actions")
            ]
            if self.workspace is not None:
                allowed += [
                    {key: "read"} for key in ("contents", "pull_requests", "actions")
                ]
            assert scope["permissions"] in allowed
            token = (
                f"synthetic-workspace-installation-{len(self.issued)}"
                if self.workspace is not None
                else "synthetic-installation-token"
            )
            self.issued[token] = scope["permissions"]
            return httpx.Response(
                201,
                json={
                    "token": token,
                    "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
                    "permissions": scope["permissions"],
                    "repositories": [{"id": REPOSITORY_ID}],
                },
            )
        assert request.method == "GET"
        installation = {
            "id": INSTALLATION_ID,
            "app_id": 7001,
            "account": {"id": 99001},
            "suspended_at": None,
        }
        if path == f"/app/installations/{INSTALLATION_ID}":
            return httpx.Response(200, json=installation)
        token = request.headers.get("authorization")
        if path.startswith(f"/repos/{REPOSITORY_NAME}"):
            if self.workspace is not None:
                return self.workspace_response(request)
            assert token == "Bearer synthetic-installation-token"
            return self.data(request)
        assert token in {
            "Bearer synthetic-acceptance-admin",
            "Bearer synthetic-acceptance-member",
        }
        if path == "/user":
            admin = token == "Bearer synthetic-acceptance-admin"
            return httpx.Response(
                200,
                json={
                    "id": ADMIN_ID if admin else MEMBER_ID,
                    "login": "acceptance-admin" if admin else "acceptance-member",
                },
            )
        assert request.url.params["per_page"] == "100"
        assert request.url.params["page"] == "1"
        if path == "/user/installations":
            return httpx.Response(200, json={"installations": [installation]})
        if path == f"/user/installations/{INSTALLATION_ID}/repositories":
            return httpx.Response(
                200,
                json={
                    "repositories": [
                        {
                            "id": REPOSITORY_ID,
                            "full_name": REPOSITORY_NAME,
                            "private": True,
                            "default_branch": "main",
                        }
                    ]
                },
            )
        raise AssertionError(f"Unexpected synthetic GitHub endpoint: {path}")

    def workspace_response(self, request):
        suffix = request.url.path.removeprefix(f"/repos/{REPOSITORY_NAME}")
        token = request.headers.get("authorization", "").removeprefix("Bearer ")
        is_user = token in {"synthetic-acceptance-admin", "synthetic-acceptance-member"}
        permissions = self.issued.get(token)
        self.workspace_requests.append(
            (suffix, "user" if is_user else sorted(permissions or {}))
        )
        metadata = (
            suffix.startswith("/git/")
            or suffix == "/pulls/2"
            or suffix
            in {"/actions/runs/81001/attempts/1", "/actions/runs/81001/attempts/2"}
        )
        if is_user:
            assert metadata
        else:
            assert permissions is not None
            required = (
                "contents"
                if suffix.startswith("/git/")
                else "actions"
                if suffix.startswith("/actions/")
                else "pull_requests"
                if suffix.startswith("/pulls/")
                else "issues"
                if suffix.startswith("/issues")
                else "metadata"
            )
            assert required in permissions or (
                suffix in {"/issues/2", "/issues/2/comments"}
                and "pull_requests" in permissions
            )
        if suffix.startswith("/git/"):
            assert not request.url.params
            item = self.workspace.metadata.get(suffix)
            return (
                httpx.Response(200, json=item)
                if item
                else httpx.Response(404, json={"message": "Not Found"})
            )
        response = self.data(request)
        if response.status_code != 200:
            return response

        def actual(value):
            if isinstance(value, list):
                return [actual(item) for item in value]
            if isinstance(value, dict):
                result = {key: actual(item) for key, item in value.items()}
                if "run_attempt" in result:
                    result.update(
                        repository={"id": REPOSITORY_ID},
                        head_repository={"id": REPOSITORY_ID},
                        pull_requests=[{"number": 2}],
                    )
                return result
            if value == "a" * 40:
                return self.workspace.commits[0]
            if value == "b" * 40:
                return self.workspace.head
            return value

        return httpx.Response(
            200,
            json=actual(response.json()),
            headers={
                key: value
                for key, value in response.headers.items()
                if key != "content-length"
            },
        )

    def data(self, request: httpx.Request) -> httpx.Response:
        """Finite source population, including real pagination and missing patches."""
        suffix = request.url.path.removeprefix(f"/repos/{REPOSITORY_NAME}")
        params = dict(request.url.params)
        stamp = self.observed.isoformat()
        user = {"id": MEMBER_ID, "login": "acceptance-member"}
        issue = {
            "id": 51001,
            "number": 1,
            "title": "Acceptance issue",
            "body": "Private issue evidence",
            "state": "open",
            "labels": [],
            "assignees": [],
            "user": user,
            "created_at": stamp,
            "updated_at": stamp,
            "closed_at": None,
        }
        second = {**issue, "id": 51003, "number": 3, "title": "Second acceptance issue"}
        pr_issue = {**issue, "id": 51002, "number": 2, "pull_request": {}}
        pull = {
            **issue,
            "id": 71002,
            "number": 2,
            "title": "Acceptance pull",
            "body": "Private pull evidence",
            "draft": False,
            "merged": False,
            "merge_commit_sha": None,
            "merged_at": None,
            "base": {"sha": "a" * 40, "ref": "main", "repo": {"id": REPOSITORY_ID}},
            "head": {"sha": "b" * 40, "ref": "feature", "repo": {"id": REPOSITORY_ID}},
        }
        comment = {
            "id": 61001,
            "body": "Acceptance comment",
            "user": user,
            "created_at": stamp,
            "updated_at": stamp,
        }
        run = {
            "id": 81001,
            "run_attempt": 2,
            "workflow_id": 41,
            "event": "push",
            "path": ".github/workflows/test.yml",
            "name": "Acceptance CI",
            "head_branch": "main",
            "head_sha": "b" * 40,
            "created_at": stamp,
            "updated_at": stamp,
            "run_started_at": stamp,
            "status": "completed",
            "conclusion": "success",
        }
        if self.actions_permission_missing and suffix.startswith("/actions/"):
            return httpx.Response(
                403, json={"message": "Resource not accessible by integration"}
            )
        list_path = suffix in {"/issues", "/pulls", "/actions/runs"} or suffix.endswith(
            ("/comments", "/reviews", "/files", "/jobs")
        )
        page = 1
        if list_path:
            assert params.pop("per_page") == "100"
            page = int(params.pop("page"))
            assert page in (1, 2)
        else:
            assert not params
        if suffix in {"/issues", "/pulls"}:
            assert params.pop("sort") == "updated"
            assert params.pop("direction") == "desc"
            state = params.pop("state")
            assert state in {"open", "closed", "all"}
            since = params.pop("since", None)
            assert not params
            if suffix == "/pulls":
                assert since is None and page == 1
                return httpx.Response(200, json=[] if state == "closed" else [pull])
            if since is not None and self.observed < datetime.fromisoformat(since):
                return httpx.Response(200, json=[])
            if page == 2:
                if self.rate_limit_page2:
                    self.rate_limit_page2 = False
                    return httpx.Response(
                        429,
                        headers={"Retry-After": "3"},
                        json={"message": "Synthetic rate limit"},
                    )
                return httpx.Response(200, json=[second])
            return httpx.Response(
                200,
                json=[issue, pr_issue],
                headers={
                    "Link": f'<{request.url.copy_set_param("page", "2")}>; rel="next"',
                },
            )
        if suffix == "/actions/runs":
            created = params.pop("created", None)
            assert not params and page == 1
            included = True
            if created:
                start, end = (datetime.fromisoformat(t) for t in created.split(".."))
                included = start <= self.observed <= end
            return httpx.Response(
                200,
                json={
                    "total_count": int(included),
                    "workflow_runs": [run] if included else [],
                },
            )
        assert not params
        details = {
            "": {"id": REPOSITORY_ID},
            "/issues/1": issue,
            "/issues/3": second,
            "/issues/2": pr_issue,
            "/pulls/2": pull,
            "/actions/runs/81001": run,
        }
        if suffix in details:
            return httpx.Response(200, json=details[suffix])
        children = {
            "/issues/1/comments": [comment],
            "/issues/3/comments": [],
            "/issues/2/comments": [{**comment, "id": 61002}],
            "/pulls/2/reviews": [
                {
                    **comment,
                    "id": 62002,
                    "state": "APPROVED",
                    "commit_id": "b" * 40,
                    "submitted_at": stamp,
                }
            ],
            "/pulls/2/comments": [
                {
                    **comment,
                    "id": 63002,
                    "pull_request_review_id": 62002,
                    "commit_id": "b" * 40,
                    "original_commit_id": "b" * 40,
                    "path": "src/example.py",
                    "side": "RIGHT",
                    "line": 1,
                }
            ],
            "/pulls/2/files": [
                {
                    "filename": "src/example.py",
                    "sha": "b" * 40,
                    "status": "modified",
                    "additions": 1,
                    "deletions": 0,
                    "changes": 1,
                }
            ],
        }
        if suffix in children:
            assert page == 1
            return httpx.Response(200, json=children[suffix])
        for attempt in (1, 2):
            attempt_path = f"/actions/runs/81001/attempts/{attempt}"
            conclusion = "failure" if attempt == 1 else "success"
            if suffix == attempt_path:
                return httpx.Response(
                    200, json={**run, "run_attempt": attempt, "conclusion": conclusion}
                )
            if suffix == attempt_path + "/jobs":
                assert page == 1
                job = {
                    "id": 91000 + attempt,
                    "run_id": 81001,
                    "run_attempt": attempt,
                    "head_sha": "b" * 40,
                    "name": "Acceptance tests",
                    "status": "completed",
                    "conclusion": conclusion,
                    "started_at": stamp,
                    "completed_at": stamp,
                    "steps": [
                        {
                            "number": 1,
                            "name": "Run checks",
                            "status": "completed",
                            "conclusion": conclusion,
                            "started_at": stamp,
                            "completed_at": stamp,
                        }
                    ],
                }
                return httpx.Response(200, json={"total_count": 1, "jobs": [job]})
        raise AssertionError(f"Unexpected synthetic data endpoint: {suffix}")


async def oauth_login(client: httpx.AsyncClient, identity: str) -> dict:
    start = await client.get("/api/v1/auth/github/start")
    assert start.status_code == 302
    assert client.cookies.get("devflow_oauth")
    state = parse_qs(urlsplit(start.headers["location"]).query)["state"][0]
    callback = await client.get(
        "/api/v1/auth/github/callback",
        params={"code": f"acceptance-{identity}", "state": state},
    )
    assert callback.status_code == 303, callback.text
    assert client.cookies.get("devflow_session")
    response = await client.get("/api/v1/session")
    assert response.status_code == 200, response.text
    return response.json()


def csrf_headers(session: dict, origin: str) -> dict[str, str]:
    return {"Origin": origin, "X-CSRF-Token": session["csrf_token"]}


class WorkspaceFixture:
    """Owned loose objects; only transfer is synthetic, never guard callbacks."""

    def __init__(self, directory: Path):
        self.directory = directory.resolve()
        self.source = self.directory / "workspace-source"
        self.root = self.directory / "workspace"
        self.directory.mkdir(parents=True, exist_ok=True)
        self.evidence = self.directory / "workspace-ownership.json"
        self.evidence.write_text(
            json.dumps(
                {
                    "pid": os.getpid(),
                    "source": str(self.source),
                    "cache": str(self.root),
                    "transfer": "synthetic",
                    "schema": None,
                    "guards": [],
                }
            ),
            encoding="utf-8",
        )
        self.source.mkdir()
        self.metadata = {}
        self.transfers = []
        self.stages = []
        self.commits = []
        self.trees = []
        self.blobs = []
        for label in ("A", "B"):
            blob = self.object("blob", f"version {label}\r\nneedle {label}\n".encode())
            subtree = self.object("tree", b"100644 example.py\0" + bytes.fromhex(blob))
            tree = self.object("tree", b"40000 src\0" + bytes.fromhex(subtree))
            commit = self.object(
                "commit",
                (
                    f"tree {tree}\nauthor Fixture <fixture@example.invalid> 0 +0000\n"
                    f"committer Fixture <fixture@example.invalid> 0 +0000\n\nVersion {label}\n"
                ).encode(),
            )
            self.commits.append(commit)
            self.trees.append(tree)
            self.blobs.append(blob)
            self.metadata[f"/git/commits/{commit}"] = {
                "sha": commit,
                "tree": {"sha": tree},
            }
        tag = self.object(
            "tag",
            (
                f"object {self.commits[0]}\ntype commit\ntag v1\n"
                "tagger Fixture <fixture@example.invalid> 0 +0000\n\nVersion one\n"
            ).encode(),
        )
        self.metadata[f"/git/tags/{tag}"] = {
            "sha": tag,
            "object": {"type": "commit", "sha": self.commits[0]},
        }
        self.set_ref("refs/heads/main", self.commits[0])
        self.set_ref("refs/tags/v1", tag, "tag")
        self.set_ref("refs/tags/light", self.commits[0])
        self.advance(0)

    def object(self, kind, body):
        raw = f"{kind} {len(body)}\0".encode() + body
        oid = hashlib.sha1(raw, usedforsecurity=False).hexdigest()
        target = self.source / "objects" / oid[:2] / oid[2:]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(zlib.compress(raw))
        return oid

    def set_ref(self, ref, oid, kind="commit"):
        target = self.source / ref
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(oid + "\n", encoding="ascii")
        if ref.startswith(("refs/heads/", "refs/tags/")):
            self.metadata["/git/ref/" + ref.removeprefix("refs/")] = {
                "ref": ref,
                "object": {"type": kind, "sha": oid},
            }

    def advance(self, index):
        self.head = self.commits[index]
        self.set_ref("refs/pull/2/head", self.head)

    @contextmanager
    def transfer_context(self):
        from devflow.workspace import handlers
        from devflow.workspace.git import GitCache
        from devflow.workspace.process import ProcessResult

        def factory(root, runner):
            assert Path(root).resolve() == self.root
            original = runner.run

            def transfer(args, **kwargs):
                if args[0] != "fetch":
                    return original(args, **kwargs)
                assert args[7] == f"https://github.com/{REPOSITORY_NAME}.git"
                destination = kwargs["cwd"].resolve()
                assert destination == self.root / f"{REPOSITORY_ID}.git"
                source_ref, target_ref = args[-1].removeprefix("+").split(":")
                assert source_ref in {
                    "refs/heads/main",
                    "refs/pull/2/head",
                    "refs/tags/v1",
                    "refs/tags/light",
                }
                assert (
                    target_ref
                    == "refs/devflow/attempts/" + runner.current.guard.operation_id
                )
                for original_object in (self.source / "objects").rglob("*"):
                    target = (
                        destination
                        / "objects"
                        / original_object.relative_to(self.source / "objects")
                    )
                    if original_object.is_file() and not target.exists():
                        target.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copyfile(original_object, target)
                target = destination / target_ref
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes((self.source / source_ref).read_bytes())
                self.transfers.append(
                    {
                        "operation_id": runner.current.guard.operation_id,
                        "ref": source_ref,
                    }
                )
                return ProcessResult("ok", returncode=0)

            runner.run = transfer
            return GitCache(root, runner)

        with patch.object(handlers, "GitCache", factory):
            yield

    async def record_guards(self, factory):
        from sqlalchemy import select, text

        from devflow.workspace.models import WorkspaceProcessGuard

        async with factory() as db:
            schema = await db.scalar(text("SELECT current_schema()"))
            rows = list(await db.scalars(select(WorkspaceProcessGuard)))
            guards = [
                {
                    "operation_id": row.operation_id,
                    "nonce": row.nonce,
                    "state": row.state,
                    "kind": row.kind,
                    "proof": row.proof,
                    "runtime_id": str(row.runtime_id),
                    "identity": row.identity,
                }
                for row in rows
            ]
        self.evidence.write_text(
            json.dumps(
                {
                    "pid": os.getpid(),
                    "source": str(self.source),
                    "cache": str(self.root),
                    "transfer": "synthetic",
                    "schema": schema,
                    "guards": guards,
                    "stages": self.stages,
                },
                default=str,
            ),
            encoding="utf-8",
        )
        return guards
