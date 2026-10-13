"""Single-attempt GitHub HTTP boundary with a ten-second total deadline."""

import asyncio
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
from pydantic import SecretStr, ValidationError

from devflow.config import Settings
from devflow.errors import AppError
from devflow.github.schemas import (
    GitHubInstallationInfo,
    GitHubRepository,
    GitHubUser,
    InstallationToken,
    TokenSet,
    UpstreamPage,
)
from devflow.security import read_secret_file, secret_configuration_error

REST_URL = "https://api.github.com"
OAUTH_URL = "https://github.com/login/oauth/access_token"
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
READ_PERMISSIONS = frozenset(
    {"issues", "pull_requests", "actions", "contents", "metadata"}
)


def unavailable() -> AppError:
    return AppError("github_unavailable", 503, "GitHub is temporarily unavailable")


def not_found() -> AppError:
    return AppError("resource_not_found", 404, "Resource not found")


def reauthorize() -> AppError:
    return AppError("reauth_required", 401, "GitHub authorization is required")


def _json(response: httpx.Response) -> dict:
    try:
        value = response.json()
        if not isinstance(value, dict):
            raise TypeError
        return value
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise unavailable() from None


def _rate_limit(response: httpx.Response) -> None:
    if response.status_code not in (403, 429):
        return
    try:
        message = _json(response).get("message", "")
    except AppError:
        message = ""
    secondary = isinstance(message, str) and (
        "secondary rate limit" in message.lower()
        or "abuse detection" in message.lower()
    )
    if not (
        response.status_code == 429
        or secondary
        or response.headers.get("x-ratelimit-remaining") == "0"
        or "retry-after" in response.headers
    ):
        return
    now = datetime.now(UTC)
    deadlines = []
    retry_after = response.headers.get("retry-after")
    if retry_after:
        try:
            if retry_after.isdecimal():
                deadlines.append(now + timedelta(seconds=int(retry_after)))
            else:
                deadline = parsedate_to_datetime(retry_after)
                if deadline.tzinfo is not None:
                    deadlines.append(deadline)
        except (ValueError, OverflowError, TypeError):
            pass
    has_retry_after = bool(deadlines)
    reset = response.headers.get("x-ratelimit-reset")
    if reset:
        try:
            deadlines.append(datetime.fromtimestamp(int(reset), UTC))
        except (ValueError, OverflowError, OSError):
            pass
    if not has_retry_after and (
        secondary or response.headers.get("x-ratelimit-remaining") != "0"
    ):
        deadlines.append(now + timedelta(seconds=60))
    retry_at = max([now, *deadlines]) if deadlines else now + timedelta(seconds=60)
    raise AppError("github_rate_limited", 503, "GitHub rate limit reached", retry_at)


def _positive_number(value: int) -> None:
    if type(value) is not int or value < 1:
        raise AppError("validation_error", 400, "A positive integer is required")


def _installation_scope(
    installation_id: int, repository_id: int, permissions: dict[str, str]
) -> None:
    _positive_number(installation_id)
    _positive_number(repository_id)
    if (
        not isinstance(permissions, dict)
        or not permissions
        or any(
            key not in READ_PERMISSIONS or value != "read"
            for key, value in permissions.items()
        )
    ):
        raise AppError(
            "validation_error", 400, "Explicit repository read permissions are required"
        )


def _next_page(response: httpx.Response, page: int) -> int | None:
    link = response.links.get("next", {}).get("url")
    if not link:
        return None
    try:
        url = urlsplit(link)
        if (
            url.scheme != "https"
            or url.hostname != "api.github.com"
            or url.port not in (None, 443)
            or url.username is not None
            or url.password is not None
            or url.fragment
            or url.path != response.request.url.path
        ):
            return None
        values = parse_qs(url.query).get("page", [])
        if len(values) != 1 or not values[0].isascii() or not values[0].isdecimal():
            return None
        next_page = int(values[0])
        return next_page if next_page > page else None
    except ValueError:
        return None


def _installation(data: dict) -> GitHubInstallationInfo:
    try:
        return GitHubInstallationInfo.model_validate(
            {
                "id": data["id"],
                "app_id": data["app_id"],
                "account_id": data["account"]["id"],
                "suspended_at": data["suspended_at"],
            }
        )
    except (KeyError, TypeError, ValidationError):
        raise unavailable() from None


class GitHubClient:
    """The caller owns the HTTP client's lifetime and platform access policy."""

    def __init__(self, http: httpx.AsyncClient, settings: Settings) -> None:
        self._http = http
        self._settings = settings

    async def _request(
        self,
        method: str,
        url: str,
        *,
        token: SecretStr | None = None,
        data: dict | None = None,
        json_body: dict | None = None,
        params: dict | None = None,
        oauth: bool = False,
        app: bool = False,
        installation: bool = False,
        allowed_statuses: tuple[int, ...] = (200,),
    ) -> httpx.Response:
        headers = {
            "Accept": "application/json" if oauth else "application/vnd.github+json"
        }
        if token is not None:
            headers["Authorization"] = f"Bearer {token.get_secret_value()}"
        try:
            async with asyncio.timeout(10):
                async with self._http.stream(
                    method,
                    url,
                    headers=headers,
                    data=data,
                    json=json_body,
                    params=params,
                    follow_redirects=False,
                    timeout=10,
                ) as upstream:
                    body = bytearray()
                    async for chunk in upstream.aiter_bytes(chunk_size=64 * 1024):
                        if len(body) + len(chunk) > MAX_RESPONSE_BYTES:
                            raise AppError(
                                "github_response_too_large",
                                503,
                                "GitHub response exceeds the size limit",
                            )
                        body.extend(chunk)
                    # aiter_bytes decodes compression; do not decode it a second time.
                    response = httpx.Response(
                        upstream.status_code,
                        headers=[
                            (key, value)
                            for key, value in upstream.headers.multi_items()
                            if key.lower() not in {"content-encoding", "content-length"}
                        ],
                        content=bytes(body),
                        request=upstream.request,
                    )
        except (httpx.HTTPError, TimeoutError):
            raise unavailable() from None
        _rate_limit(response)
        if response.status_code >= 500 or 300 <= response.status_code < 400:
            raise unavailable()
        if not oauth:
            if response.status_code == 401:
                if app:
                    raise secret_configuration_error()
                if installation:
                    raise AppError(
                        "github_installation_unavailable",
                        503,
                        "GitHub installation access is unavailable",
                    )
                raise reauthorize()
            if installation and response.status_code == 403:
                raise AppError(
                    "permission_missing",
                    403,
                    "GitHub installation permission is required",
                )
            if response.status_code in (403, 404):
                raise not_found()
            if response.status_code not in allowed_statuses:
                raise unavailable()
        return response

    def _oauth_credentials(self) -> dict[str, str]:
        if not self._settings.github_client_id:
            raise secret_configuration_error()
        try:
            secret = read_secret_file(self._settings.github_client_secret_file).decode(
                "utf-8"
            )
        except UnicodeError:
            raise secret_configuration_error() from None
        return {"client_id": self._settings.github_client_id, "client_secret": secret}

    async def _oauth(self, data: dict[str, str], *, refresh: bool = False) -> TokenSet:
        response = await self._request("POST", OAUTH_URL, data=data, oauth=True)
        payload = _json(response)
        error = payload.get("error")
        if error in (
            "bad_refresh_token",
            "bad_verification_code",
            "expired_token",
            "access_denied",
        ):
            raise reauthorize()
        if error in ("incorrect_client_credentials", "redirect_uri_mismatch"):
            raise secret_configuration_error()
        if error is not None or response.status_code != 200:
            raise unavailable()
        try:
            access = payload["access_token"]
            rotated = payload.get("refresh_token")
            if payload.get("token_type", "bearer") != "bearer":
                raise ValueError
            if not isinstance(access, str) or not access:
                raise ValueError
            if rotated is not None and (not isinstance(rotated, str) or not rotated):
                raise ValueError
            if refresh and rotated is None:
                raise ValueError
            now = datetime.now(UTC)

            def expiry(name: str) -> datetime | None:
                seconds = payload.get(name)
                if seconds is None:
                    return None
                if type(seconds) is not int or seconds <= 0:
                    raise ValueError
                return now + timedelta(seconds=seconds)

            expires_at = expiry("expires_in")
            refresh_expires_at = expiry("refresh_token_expires_in")
            if refresh_expires_at is not None and rotated is None:
                raise ValueError
            if expires_at is not None and (
                rotated is None or refresh_expires_at is None
            ):
                raise ValueError
            if rotated is not None and (
                expires_at is None or refresh_expires_at is None
            ):
                raise ValueError
            return TokenSet(
                access_token=SecretStr(access),
                refresh_token=SecretStr(rotated) if rotated else None,
                expires_at=expires_at,
                refresh_expires_at=refresh_expires_at,
            )
        except (KeyError, ValueError, TypeError, OverflowError):
            raise unavailable() from None

    async def exchange_code(self, code: SecretStr, verifier: SecretStr) -> TokenSet:
        return await self._oauth(
            {
                **self._oauth_credentials(),
                "code": code.get_secret_value(),
                "code_verifier": verifier.get_secret_value(),
            }
        )

    async def refresh_token(self, refresh: SecretStr) -> TokenSet:
        return await self._oauth(
            {
                **self._oauth_credentials(),
                "grant_type": "refresh_token",
                "refresh_token": refresh.get_secret_value(),
            },
            refresh=True,
        )

    async def get_user(self, token: SecretStr) -> GitHubUser:
        response = await self._request("GET", f"{REST_URL}/user", token=token)
        try:
            return GitHubUser.model_validate(_json(response))
        except ValidationError:
            raise unavailable() from None

    async def list_installations(
        self, token: SecretStr, page: int
    ) -> UpstreamPage[GitHubInstallationInfo]:
        _positive_number(page)
        response = await self._request(
            "GET",
            f"{REST_URL}/user/installations",
            token=token,
            params={"page": page, "per_page": 100},
        )
        items = _json(response).get("installations")
        if not isinstance(items, list):
            raise unavailable()
        return UpstreamPage(
            items=[_installation(item) for item in items],
            next_page=_next_page(response, page),
        )

    async def list_repositories(
        self, token: SecretStr, installation_id: int, page: int
    ) -> UpstreamPage[GitHubRepository]:
        _positive_number(installation_id)
        _positive_number(page)
        response = await self._request(
            "GET",
            f"{REST_URL}/user/installations/{installation_id}/repositories",
            token=token,
            params={"page": page, "per_page": 100},
        )
        items = _json(response).get("repositories")
        if not isinstance(items, list):
            raise unavailable()
        try:
            return UpstreamPage(
                items=[GitHubRepository.model_validate(item) for item in items],
                next_page=_next_page(response, page),
            )
        except ValidationError:
            raise unavailable() from None

    def _app_token(self) -> SecretStr:
        if not self._settings.github_app_id or not self._settings.github_client_id:
            raise secret_configuration_error()
        private_key = read_secret_file(self._settings.github_private_key_file)
        now = int(datetime.now(UTC).timestamp())
        try:
            token = SecretStr(
                jwt.encode(
                    {
                        "iat": now - 60,
                        "exp": now + 540,
                        "iss": self._settings.github_client_id,
                    },
                    private_key,
                    algorithm="RS256",
                )
            )
        except (jwt.PyJWTError, ValueError, TypeError):
            raise secret_configuration_error() from None
        return token

    async def create_installation_token(
        self, installation_id: int, repository_id: int, permissions: dict[str, str]
    ) -> InstallationToken:
        _installation_scope(installation_id, repository_id, permissions)
        response = await self._request(
            "POST",
            f"{REST_URL}/app/installations/{installation_id}/access_tokens",
            token=self._app_token(),
            json_body={"repository_ids": [repository_id], "permissions": permissions},
            app=True,
            installation=True,
            allowed_statuses=(201,),
        )
        payload = _json(response)
        try:
            result = InstallationToken.model_validate(
                {key: payload[key] for key in ("token", "expires_at", "permissions")}
            )
            if (
                not result.token.get_secret_value()
                or result.expires_at <= datetime.now(UTC)
                or any(
                    result.permissions.get(key) != value
                    for key, value in permissions.items()
                )
                or any(
                    key not in {*permissions, "metadata"} or value != "read"
                    for key, value in result.permissions.items()
                )
            ):
                raise ValueError
            repositories = payload.get("repositories")
            if repositories is not None and (
                not isinstance(repositories, list)
                or len(repositories) != 1
                or not isinstance(repositories[0], dict)
                or type(repositories[0].get("id")) is not int
                or repositories[0]["id"] != repository_id
            ):
                raise ValueError
            return result
        except (KeyError, ValueError, TypeError, ValidationError):
            raise unavailable() from None

    async def get_installation(self, installation_id: int) -> GitHubInstallationInfo:
        _positive_number(installation_id)
        response = await self._request(
            "GET",
            f"{REST_URL}/app/installations/{installation_id}",
            token=self._app_token(),
            app=True,
        )
        item = _installation(_json(response))
        if (
            item.id != installation_id
            or item.app_id != self._settings.github_app_id
            or item.suspended_at is not None
        ):
            raise not_found()
        return item
