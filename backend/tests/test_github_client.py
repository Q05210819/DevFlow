import asyncio
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from urllib.parse import parse_qs

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from pydantic import SecretStr

from devflow.errors import AppError

pytestmark = pytest.mark.asyncio
TOKEN = SecretStr("access-sentinel")


@pytest.fixture
def github_settings(settings_factory, workspace_tmp_path):
    secret = workspace_tmp_path / "client-secret"
    secret.write_text("client-secret-sentinel")
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    key_file = workspace_tmp_path / "private-key"
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
        github_client_secret_file=secret,
        github_private_key_file=key_file,
        github_allowed_installation_ids=[42],
    )


def installation(**changes):
    return {
        "id": 42,
        "app_id": 123,
        "account": {"id": 7},
        "suspended_at": None,
        **changes,
    }


async def test_oauth_exchange_and_refresh_contract(github_settings):
    from devflow.github.client import GitHubClient

    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "access_token": "new-access-sentinel",
                "refresh_token": "new-refresh-sentinel",
                "expires_in": 28800,
                "refresh_token_expires_in": 15897600,
                "token_type": "bearer",
                "scope": "",
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        github = GitHubClient(http, github_settings)
        before = datetime.now(UTC)
        exchanged = await github.exchange_code(
            SecretStr("code-sentinel"), SecretStr("verifier-sentinel")
        )
        refreshed = await github.refresh_token(SecretStr("refresh-sentinel"))
    for result in (exchanged, refreshed):
        assert result.access_token.get_secret_value() == "new-access-sentinel"
        assert result.refresh_token.get_secret_value() == "new-refresh-sentinel"
        assert (
            before + timedelta(seconds=28800)
            <= result.expires_at
            <= datetime.now(UTC) + timedelta(seconds=28800)
        )
        assert result.refresh_expires_at >= before + timedelta(seconds=15897600)
        assert "new-access-sentinel" not in repr(result)
        assert "new-refresh-sentinel" not in result.model_dump_json()
    for request in requests:
        assert request.method == "POST"
        assert str(request.url) == "https://github.com/login/oauth/access_token"
        assert request.headers["accept"] == "application/json"
    assert parse_qs(requests[0].content.decode()) == {
        "client_id": ["client-id"],
        "client_secret": ["client-secret-sentinel"],
        "code": ["code-sentinel"],
        "code_verifier": ["verifier-sentinel"],
    }
    assert parse_qs(requests[1].content.decode()) == {
        "client_id": ["client-id"],
        "client_secret": ["client-secret-sentinel"],
        "grant_type": ["refresh_token"],
        "refresh_token": ["refresh-sentinel"],
    }


async def test_rest_endpoints_user_tokens_and_app_jwt(github_settings):
    from devflow.github.client import GitHubClient

    seen = []

    def handle(request):
        seen.append(request)
        assert request.url.host == "api.github.com"
        if request.url.path == "/user":
            return httpx.Response(
                200, json={"id": 7, "login": "octocat", "avatar_url": None}
            )
        if request.url.path == "/user/installations":
            return httpx.Response(200, json={"installations": [installation()]})
        if request.url.path == "/user/installations/42/repositories":
            return httpx.Response(
                200,
                json={
                    "repositories": [
                        {
                            "id": 81,
                            "full_name": "team/private",
                            "private": True,
                            "default_branch": "main",
                        }
                    ]
                },
            )
        assert request.url.path == "/app/installations/42"
        return httpx.Response(200, json=installation())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        github = GitHubClient(http, github_settings)
        assert (await github.get_user(TOKEN)).login == "octocat"
        assert (await github.list_installations(TOKEN, 2)).items[0].account_id == 7
        assert (await github.list_repositories(TOKEN, 42, 3)).items[0].private is True
        assert (await github.get_installation(42)).id == 42
    assert len(seen) == 4
    assert all(r.headers["authorization"] == "Bearer access-sentinel" for r in seen[:3])
    assert seen[1].url.params["page"] == "2"
    assert seen[2].url.params["page"] == "3"
    private = serialization.load_pem_private_key(
        github_settings.github_private_key_file.read_bytes(), None
    )
    claims = jwt.decode(
        seen[3].headers["authorization"].removeprefix("Bearer "),
        private.public_key(),
        algorithms=["RS256"],
    )
    assert claims["iss"] == "client-id"
    assert 0 < claims["exp"] - datetime.now(UTC).timestamp() <= 600
    assert claims["iat"] <= datetime.now(UTC).timestamp() - 50


@pytest.mark.parametrize(
    ("status", "headers", "body", "code"),
    [
        (
            403,
            {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "4102444800"},
            {"message": "limit"},
            "github_rate_limited",
        ),
        (403, {"retry-after": "120"}, {"message": "limit"}, "github_rate_limited"),
        (
            403,
            {},
            {"message": "You have exceeded a secondary rate limit."},
            "github_rate_limited",
        ),
        (429, {}, {"message": "limit"}, "github_rate_limited"),
        (403, {}, {"message": "Resource not accessible"}, "resource_not_found"),
        (404, {}, {}, "resource_not_found"),
        (401, {}, {}, "reauth_required"),
    ],
)
async def test_github_rate_limit_classification(
    github_settings, status, headers, body, code
):
    from devflow.github.client import GitHubClient

    before = datetime.now(UTC)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(status, headers=headers, json=body)
        )
    ) as http:
        with pytest.raises(AppError) as caught:
            await GitHubClient(http, github_settings).get_user(TOKEN)
    assert caught.value.code == code
    if code == "github_rate_limited":
        deadline = before + timedelta(seconds=int(headers.get("retry-after", "60")))
        if "x-ratelimit-reset" in headers:
            deadline = datetime(2100, 1, 1, tzinfo=UTC)
        assert caught.value.retry_at >= deadline


async def test_rate_limit_uses_latest_provider_deadline(github_settings):
    from devflow.github.client import GitHubClient

    deadline = datetime(2100, 1, 2, tzinfo=UTC)
    headers = {
        "retry-after": format_datetime(deadline),
        "x-ratelimit-reset": "4102444800",
        "x-ratelimit-remaining": "0",
    }
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(429, headers=headers))
    ) as http:
        with pytest.raises(AppError) as caught:
            await GitHubClient(http, github_settings).get_user(TOKEN)
    assert caught.value.code == "github_rate_limited"
    assert caught.value.retry_at >= deadline


@pytest.mark.parametrize(
    ("status", "body", "code"),
    [
        (200, {"error": "bad_refresh_token"}, "reauth_required"),
        (400, {"error": "bad_refresh_token"}, "reauth_required"),
        (200, {"error": "incorrect_client_credentials"}, "github_configuration_error"),
        (401, {"error": "incorrect_client_credentials"}, "github_configuration_error"),
        (503, {"error": "bad_refresh_token"}, "github_unavailable"),
        (
            200,
            {"error": "unknown", "error_description": "secret-sentinel"},
            "github_unavailable",
        ),
        (
            200,
            {"access_token": "partial-secret", "expires_in": 28800},
            "github_unavailable",
        ),
    ],
)
async def test_refresh_failure_classification(
    github_settings, status, body, code, caplog
):
    from devflow.github.client import GitHubClient

    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(status, json=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        with pytest.raises(AppError) as caught:
            await GitHubClient(http, github_settings).refresh_token(
                SecretStr("refresh-sentinel")
            )
    assert caught.value.code == code
    assert len(requests) == 1
    installation_fallback_calls = sum("/access_tokens" in r.url.path for r in requests)
    assert installation_fallback_calls == 0
    assert "sentinel" not in str(caught.value) + caplog.text


async def test_timeout_is_transient_without_retry(github_settings):
    from devflow.github.client import GitHubClient

    calls = []

    def handle(request):
        calls.append(request)
        raise httpx.ReadTimeout("refresh-sentinel", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        with pytest.raises(AppError) as caught:
            await GitHubClient(http, github_settings).refresh_token(
                SecretStr("refresh-sentinel")
            )
    assert caught.value.code == "github_unavailable"
    assert len(calls) == 1
    assert "sentinel" not in str(caught.value)


async def test_ten_second_total_deadline(github_settings):
    from devflow.github.client import GitHubClient

    cancelled = asyncio.Event()

    async def handle(request):
        try:
            await asyncio.sleep(11)
        finally:
            cancelled.set()
        return httpx.Response(200, json={"id": 7, "login": "late"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handle), timeout=None
    ) as http:
        start = asyncio.get_running_loop().time()
        with pytest.raises(AppError) as caught:
            await GitHubClient(http, github_settings).get_user(TOKEN)
        elapsed = asyncio.get_running_loop().time() - start
    assert caught.value.code == "github_unavailable"
    assert 9.5 <= elapsed < 10.8
    assert cancelled.is_set()


@pytest.mark.parametrize(
    "change", [{"id": 43}, {"app_id": 999}, {"suspended_at": "2026-01-01T00:00:00Z"}]
)
async def test_reject_wrong_or_suspended_installation(github_settings, change):
    from devflow.github.client import GitHubClient

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json=installation(**change))
        )
    ) as http:
        with pytest.raises(AppError) as caught:
            await GitHubClient(http, github_settings).get_installation(42)
    assert caught.value.code == "resource_not_found"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://api.github.com/user/installations?page=2", 2),
        ("https://attacker.invalid/user/installations?page=2", None),
        ("https://api.github.com.attacker.invalid/user/installations?page=2", None),
        ("http://api.github.com/user/installations?page=2", None),
        ("https://user:secret@api.github.com/user/installations?page=2", None),
        ("https://api.github.com:444/user/installations?page=2", None),
        ("https://api.github.com/user/installations?page=-1", None),
        ("https://api.github.com/user/installations?page=1", None),
        ("https://api.github.com/user/installations?page=2&page=3", None),
        ("https://api.github.com/other?page=2", None),
    ],
)
async def test_trusted_pagination_preserves_unfiltered_items(
    github_settings, url, expected
):
    from devflow.github.client import GitHubClient

    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(
            200,
            headers={"link": f'<{url}>; rel="next"'},
            json={"installations": [installation(app_id=999)]},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        result = await GitHubClient(http, github_settings).list_installations(TOKEN, 1)
    assert result.items[0].app_id == 999
    assert result.next_page == expected
    assert len(requests) == 1


@pytest.mark.parametrize(
    "payload", [[], {"id": "secret-sentinel"}, {"id": True, "login": "octocat"}]
)
async def test_malformed_upstream_is_controlled(github_settings, payload):
    from devflow.github.client import GitHubClient

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=payload))
    ) as http:
        with pytest.raises(AppError) as caught:
            await GitHubClient(http, github_settings).get_user(TOKEN)
    assert caught.value.code == "github_unavailable"
    assert "secret-sentinel" not in str(caught.value)


async def test_redirects_are_not_followed(github_settings):
    from devflow.github.client import GitHubClient

    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(
            307, headers={"location": "https://attacker.invalid/steal"}
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handle), follow_redirects=True
    ) as http:
        with pytest.raises(AppError) as caught:
            await GitHubClient(http, github_settings).get_user(TOKEN)
    assert caught.value.code == "github_unavailable"
    assert len(requests) == 1


async def test_secondary_limit_without_retry_after_waits_a_minute(github_settings):
    from devflow.github.client import GitHubClient

    before = datetime.now(UTC)
    headers = {
        "x-ratelimit-remaining": "20",
        "x-ratelimit-reset": str(int((before + timedelta(seconds=5)).timestamp())),
    }
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(
                403,
                headers=headers,
                json={"message": "You have exceeded a secondary rate limit."},
            )
        )
    ) as http:
        with pytest.raises(AppError) as caught:
            await GitHubClient(http, github_settings).get_user(TOKEN)
    assert caught.value.code == "github_rate_limited"
    assert caught.value.retry_at >= before + timedelta(seconds=60)


@pytest.mark.parametrize(
    "payload",
    [
        {"access_token": "access-sentinel", "refresh_token_expires_in": 42},
        {"access_token": "access-sentinel", "token_type": "mac"},
    ],
)
async def test_exchange_rejects_incoherent_token_response(github_settings, payload):
    from devflow.github.client import GitHubClient

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=payload))
    ) as http:
        with pytest.raises(AppError) as caught:
            await GitHubClient(http, github_settings).exchange_code(
                SecretStr("code"), SecretStr("verifier")
            )
    assert caught.value.code == "github_unavailable"
