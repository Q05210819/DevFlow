"""Acceptance fixtures: real application state, synthetic GitHub HTTP only."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
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
        **paths,
    )


class SyntheticGitHub:
    """Codes select a synthetic identity; production OAuth still validates state."""

    def __init__(self):
        self.observed = datetime.now(UTC).replace(microsecond=0) - timedelta(minutes=1)
        self.rate_limit_page2 = False
        self.actions_permission_missing = False

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
            assert scope["permissions"] in [
                {"metadata": "read", key: "read"}
                for key in ("issues", "pull_requests", "actions")
            ]
            return httpx.Response(
                201,
                json={
                    "token": "synthetic-installation-token",
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
