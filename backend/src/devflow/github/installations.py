"""Bounded in-memory installation tokens, isolated by scope and authorization epoch."""

from collections import OrderedDict
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

from pydantic import SecretStr

from devflow.errors import AppError
from devflow.github.client import GitHubClient, _installation_scope
from devflow.github.schemas import InstallationToken


class InstallationTokens:
    def __init__(self, client: GitHubClient) -> None:
        self._client = client
        self._cache: OrderedDict[tuple, InstallationToken] = OrderedDict()

    async def get(
        self,
        installation_id: int,
        repository_id: int,
        permissions: dict[str, str],
        *,
        auth_version: tuple[int, int],
        before_request: Callable[[], Awaitable[None]],
    ) -> SecretStr:
        _installation_scope(installation_id, repository_id, permissions)
        if (
            not isinstance(auth_version, tuple)
            or len(auth_version) != 2
            or any(type(value) is not int or value < 0 for value in auth_version)
        ):
            raise AppError(
                "validation_error", 400, "An authorization version is required"
            )
        permissions = dict(permissions)
        key = (
            installation_id,
            repository_id,
            tuple(sorted(permissions.items())),
            auth_version,
        )
        cached = self._cache.get(key)
        if cached is not None and cached.expires_at > datetime.now(UTC) + timedelta(
            seconds=60
        ):
            self._cache.move_to_end(key)
            return cached.token
        self._cache.pop(key, None)
        # The sync executor serializes requests for an installation; no HTTP retries here.
        await before_request()
        issued = await self._client.create_installation_token(
            installation_id, repository_id, permissions
        )
        self._cache[key] = issued
        self._cache.move_to_end(key)
        while len(self._cache) > 128:
            self._cache.popitem(last=False)
        return issued.token
