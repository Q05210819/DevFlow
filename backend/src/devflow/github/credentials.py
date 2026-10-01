"""Persistent user credentials shared by API and worker processes.

Every path locks User before GitHubCredential. The database lock spans the
client's bounded refresh request, so a waiting process rereads the new pair.
"""

import hmac

from pydantic import SecretStr
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from devflow.audit.service import AuditEntry, write_audit
from devflow.auth.models import AUTH_ORDER_SEQUENCE, User
from devflow.auth.types import Actor
from devflow.errors import AppError
from devflow.github.client import GitHubClient, reauthorize
from devflow.github.models import GitHubCredential
from devflow.security import CredentialCipher


class CredentialService:
    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        github: GitHubClient,
        cipher: CredentialCipher,
    ) -> None:
        self.factory = factory
        self.github = github
        self.cipher = cipher

    async def _lock_user(self, session: AsyncSession, actor: Actor) -> User | None:
        user = await session.scalar(
            select(User).where(User.id == actor.id).with_for_update()
        )
        if (
            user is None
            or not user.enabled
            or user.auth_version != actor.auth_version
            or user.github_id != actor.github_id
        ):
            return None
        return user

    async def _lock_credential(
        self, session: AsyncSession, user: User
    ) -> GitHubCredential | None:
        return await session.scalar(
            select(GitHubCredential)
            .where(GitHubCredential.user_id == user.id)
            .with_for_update()
        )

    async def _revoke(
        self, session: AsyncSession, user: User, credential: GitHubCredential
    ) -> None:
        # Caller holds both locks. Invalidate sessions and OAuth states predating
        # this authorization change without changing membership or admin roles.
        credential.access_token_encrypted = None
        credential.refresh_token_encrypted = None
        credential.expires_at = None
        credential.refresh_expires_at = None
        credential.needs_reauth = True
        credential.version += 1
        user.auth_version += 1
        user.auth_changed_at = func.clock_timestamp()
        user.auth_changed_order = AUTH_ORDER_SEQUENCE.next_value()
        await write_audit(
            session,
            AuditEntry(
                actor_id=user.id,
                action="credential.revoked",
                resource_type="credential",
                resource_id=credential.id,
                outcome="success",
                reason_code="reauth_required",
            ),
        )

    async def get_user_token(self, actor: Actor) -> SecretStr:
        """Return current access or rotate once, always using fresh locked rows.

        Terminal refresh rejection is committed with its audit before raising
        reauth_required. Configuration and transient failures preserve the pair.
        """
        async with self.factory.begin() as session:
            user = await self._lock_user(session, actor)
            if user is None:
                raise AppError("unauthenticated", 401, "Authentication required")
            credential = await self._lock_credential(session, user)
            if (
                credential is None
                or credential.needs_reauth
                or credential.access_token_encrypted is None
            ):
                raise reauthorize()

            now = await session.scalar(select(func.clock_timestamp()))
            if credential.expires_at is None or credential.expires_at > now:
                return self.cipher.decrypt(credential.access_token_encrypted)

            if credential.refresh_token_encrypted is None or (
                credential.refresh_expires_at is not None
                and credential.refresh_expires_at <= now
            ):
                await self._revoke(session, user, credential)
            else:
                refresh = self.cipher.decrypt(credential.refresh_token_encrypted)
                try:
                    tokens = await self.github.refresh_token(refresh)
                except AppError as error:
                    if error.code != "reauth_required":
                        raise
                    await self._revoke(session, user, credential)
                else:
                    # GitHubClient validates a complete refresh response. Encrypt
                    # both before assignment; any failure rolls back this pair.
                    access = self.cipher.encrypt(tokens.access_token)
                    refresh = self.cipher.encrypt(tokens.refresh_token)
                    credential.access_token_encrypted = access
                    credential.refresh_token_encrypted = refresh
                    credential.expires_at = tokens.expires_at
                    credential.refresh_expires_at = tokens.refresh_expires_at
                    credential.version += 1
                    return tokens.access_token
        raise reauthorize()

    async def invalidate_user_token(
        self, actor: Actor, observed_token: SecretStr
    ) -> bool:
        """Report a confirmed REST401 using the exact token sent upstream.

        Return False without mutation for stale actors/tokens or revoked state.
        Return True after atomically revoking the still-current token and audit.
        Callers must only use this for terminal errors, never 403/config/timeout.
        """
        async with self.factory.begin() as session:
            user = await self._lock_user(session, actor)
            if user is None:
                return False
            credential = await self._lock_credential(session, user)
            if (
                credential is None
                or credential.needs_reauth
                or credential.access_token_encrypted is None
            ):
                return False
            current = self.cipher.decrypt(credential.access_token_encrypted)
            if not hmac.compare_digest(
                current.get_secret_value().encode(),
                observed_token.get_secret_value().encode(),
            ):
                return False
            await self._revoke(session, user, credential)
            return True
