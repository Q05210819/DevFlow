import json
from datetime import UTC, datetime, timedelta

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from devflow.errors import AppError
from devflow.github.client import GitHubClient

pytestmark = pytest.mark.asyncio


@pytest.fixture
def github_settings(settings_factory, tmp_path):
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    key_file = tmp_path / "private-key"
    key_file.write_bytes(
        private.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return settings_factory(
        github_app_id=123,
        github_client_id="client-id",
        github_private_key_file=key_file,
    )


def token_payload(**changes):
    return {
        "token": "installation-secret-sentinel",
        "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        "permissions": {"issues": "read", "metadata": "read"},
        "repositories": [{"id": 81}],
        **changes,
    }


async def test_token_is_repository_and_permission_scoped(github_settings):
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(201, json=token_payload())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        result = await GitHubClient(http, github_settings).create_installation_token(
            42, 81, {"issues": "read"}
        )
    assert requests[0].method == "POST"
    assert (
        str(requests[0].url)
        == "https://api.github.com/app/installations/42/access_tokens"
    )
    assert json.loads(requests[0].content) == {
        "repository_ids": [81],
        "permissions": {"issues": "read"},
    }
    private = serialization.load_pem_private_key(
        github_settings.github_private_key_file.read_bytes(), None
    )
    claims = jwt.decode(
        requests[0].headers["authorization"].removeprefix("Bearer "),
        private.public_key(),
        algorithms=["RS256"],
    )
    assert claims["iss"] == "client-id"
    assert result.token.get_secret_value() == "installation-secret-sentinel"
    assert result.expires_at.tzinfo is not None
    assert "installation-secret-sentinel" not in repr(result) + result.model_dump_json()


async def test_installation_token_expiry_is_normalized_to_utc(github_settings):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(
                201, json=token_payload(expires_at="2100-01-01T08:00:00+08:00")
            )
        )
    ) as http:
        result = await GitHubClient(http, github_settings).create_installation_token(
            42, 81, {"issues": "read"}
        )
    assert result.expires_at.isoformat() == "2100-01-01T00:00:00+00:00"


@pytest.mark.parametrize(
    "changes",
    [
        {"token": ""},
        {"token": 123},
        {"expires_at": "bad"},
        {"expires_at": "2100-01-01T00:00:00"},
        {"expires_at": "2020-01-01T00:00:00Z"},
        {"permissions": {"issues": "write"}},
        {"permissions": {"metadata": "read"}},
        {"permissions": {"issues": "read", "contents": "write"}},
        {"repositories": [{"id": 82}]},
        {"repositories": [{"id": 81}, {"id": 82}]},
    ],
)
async def test_invalid_token_response_is_not_cached(github_settings, changes):
    from devflow.github.installations import InstallationTokens

    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(201, json=token_payload(**changes))

    async def before_request():
        pass

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        tokens = InstallationTokens(GitHubClient(http, github_settings))
        for _ in range(2):
            with pytest.raises(AppError) as caught:
                await tokens.get(
                    42,
                    81,
                    {"issues": "read"},
                    auth_version=(1, 1),
                    before_request=before_request,
                )
            assert caught.value.code == "github_unavailable"
            assert "sentinel" not in str(caught.value)
    assert len(calls) == 2


async def test_token_json_recursion_failure_is_controlled_and_not_cached(
    github_settings,
):
    from devflow.github.installations import InstallationTokens

    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(201, content=b"[" * 50000 + b"]" * 50000)

    async def before_request():
        pass

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        tokens = InstallationTokens(GitHubClient(http, github_settings))
        for _ in range(2):
            with pytest.raises(AppError) as caught:
                await tokens.get(
                    42,
                    81,
                    {"issues": "read"},
                    auth_version=(1, 1),
                    before_request=before_request,
                )
            assert caught.value.code == "github_unavailable"
    assert len(calls) == 2


async def test_cache_is_scoped_by_installation_repository_permissions_and_epoch(
    github_settings,
):
    from devflow.github.installations import InstallationTokens

    events = []

    def handle(request):
        events.append("http")
        body = json.loads(request.content)
        return httpx.Response(
            201,
            json=token_payload(
                permissions={**body["permissions"], "metadata": "read"},
                repositories=[{"id": body["repository_ids"][0]}],
                token=f"token-{events.count('http')}",
            ),
        )

    async def before_request():
        events.append("budget")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        tokens = InstallationTokens(GitHubClient(http, github_settings))
        first = await tokens.get(
            42,
            81,
            {"issues": "read"},
            auth_version=(1, 1),
            before_request=before_request,
        )
        hit = await tokens.get(
            42,
            81,
            {"issues": "read"},
            auth_version=(1, 1),
            before_request=before_request,
        )
        assert first == hit
        assert events == ["budget", "http"]
        for installation, repository, permissions, version in [
            (43, 81, {"issues": "read"}, (1, 1)),
            (42, 82, {"issues": "read"}, (1, 1)),
            (42, 81, {"actions": "read"}, (1, 1)),
            (42, 81, {"issues": "read"}, (2, 1)),
            (42, 81, {"issues": "read"}, (1, 2)),
        ]:
            assert (
                await tokens.get(
                    installation,
                    repository,
                    permissions,
                    auth_version=version,
                    before_request=before_request,
                )
                != first
            )
    assert events == ["budget", "http"] * 6


async def test_refreshes_sixty_seconds_before_expiry(github_settings, monkeypatch):
    from devflow.github import installations

    now = datetime(2026, 10, 13, tzinfo=UTC)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now

    monkeypatch.setattr(installations, "datetime", Clock)
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(
            201,
            json=token_payload(
                expires_at="2100-01-01T00:00:00Z", token=f"token-{len(calls)}"
            ),
        )

    async def before_request():
        pass

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        tokens = installations.InstallationTokens(GitHubClient(http, github_settings))
        first = await tokens.get(
            42,
            81,
            {"issues": "read"},
            auth_version=(1, 1),
            before_request=before_request,
        )
        now = datetime(2100, 1, 1, tzinfo=UTC) - timedelta(seconds=61)
        assert (
            await tokens.get(
                42,
                81,
                {"issues": "read"},
                auth_version=(1, 1),
                before_request=before_request,
            )
            == first
        )
        now += timedelta(seconds=1)
        assert (
            await tokens.get(
                42,
                81,
                {"issues": "read"},
                auth_version=(1, 1),
                before_request=before_request,
            )
            != first
        )
    assert len(calls) == 2


async def test_cache_evicts_old_entries_at_128(github_settings):
    from devflow.github.installations import InstallationTokens

    calls = []

    def handle(request):
        calls.append(request)
        body = json.loads(request.content)
        return httpx.Response(
            201,
            json=token_payload(
                repositories=[{"id": body["repository_ids"][0]}],
                token=f"token-{len(calls)}",
            ),
        )

    async def before_request():
        pass

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        tokens = InstallationTokens(GitHubClient(http, github_settings))
        for repository in range(1, 130):
            await tokens.get(
                42,
                repository,
                {"issues": "read"},
                auth_version=(1, 1),
                before_request=before_request,
            )
        await tokens.get(
            42,
            129,
            {"issues": "read"},
            auth_version=(1, 1),
            before_request=before_request,
        )
        await tokens.get(
            42,
            1,
            {"issues": "read"},
            auth_version=(1, 1),
            before_request=before_request,
        )
    assert len(calls) == 130


async def test_budget_failure_prevents_token_http(github_settings):
    from devflow.github.installations import InstallationTokens

    calls = []

    async def before_request():
        raise AppError("attempt_timeout", 503, "Budget exhausted")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: calls.append(r))
    ) as http:
        with pytest.raises(AppError) as caught:
            await InstallationTokens(GitHubClient(http, github_settings)).get(
                42,
                81,
                {"issues": "read"},
                auth_version=(1, 1),
                before_request=before_request,
            )
    assert caught.value.code == "attempt_timeout"
    assert calls == []


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (401, "github_configuration_error"),
        (403, "permission_missing"),
        (404, "resource_not_found"),
        (200, "github_unavailable"),
    ],
)
async def test_issuance_errors_preserve_app_auth_semantics(
    github_settings, status, expected
):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(status, json=token_payload())
        )
    ) as http:
        with pytest.raises(AppError) as caught:
            await GitHubClient(http, github_settings).create_installation_token(
                42, 81, {"issues": "read"}
            )
    assert caught.value.code == expected


@pytest.mark.parametrize(
    "permissions",
    [{}, {"issues": "write"}, {"administration": "write"}, {"issues": True}],
)
async def test_token_requests_cannot_grant_write_or_default_all_permissions(
    github_settings, permissions
):
    calls = []
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: calls.append(r))
    ) as http:
        with pytest.raises(AppError) as caught:
            await GitHubClient(http, github_settings).create_installation_token(
                42, 81, permissions
            )
    assert caught.value.code == "validation_error"
    assert calls == []
