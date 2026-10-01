"""Database-clock authentication and serialized membership changes."""

import base64
import hashlib
import re
from datetime import datetime, timedelta
from urllib.parse import urlencode
from uuid import UUID

from pydantic import SecretStr
from sqlalchemy import delete, func, select, tuple_, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from devflow.audit.models import AuditLog
from devflow.audit.service import AuditEntry, ReasonCode, write_audit
from devflow.auth.models import OAuthState, Session, User
from devflow.auth.schemas import AuditView, LoginStart, SessionIssue, UserView
from devflow.auth.types import Actor, Role
from devflow.config import Settings
from devflow.db import lock_admin_set
from devflow.errors import AppError
from devflow.github.client import GitHubClient
from devflow.github.models import GitHubCredential
from devflow.pagination import CursorCodec, Page
from devflow.security import (
    CredentialCipher,
    csrf_token,
    random_token,
    read_secret_file,
    secret_configuration_error,
    token_hash,
)


def unauthenticated() -> AppError:
    return AppError("unauthenticated", 401, "Authentication required")


def forbidden() -> AppError:
    return AppError("forbidden", 403, "Forbidden")


def validate_return_path(value: str) -> str:
    if value in {"/", "/repositories", "/admin/users", "/admin/audit"} or re.fullmatch(
        r"/jobs/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", value
    ):
        return value
    raise AppError("validation_error", 400, "Invalid return path")


class AuthService:
    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        github: GitHubClient,
        cipher: CredentialCipher,
        settings: Settings,
    ):
        self.factory, self.github, self.cipher, self.settings = (
            factory,
            github,
            cipher,
            settings,
        )

    def signing_key(self) -> bytes:
        key = read_secret_file(self.settings.app_signing_key_file)
        if len(key) < 32:
            raise secret_configuration_error()
        return key

    def session_csrf(self, raw_token: str) -> str:
        return csrf_token(self.signing_key(), raw_token)

    async def audit_denial(
        self, reason: ReasonCode, actor: Actor | None = None
    ) -> None:
        async with self.factory.begin() as db:
            await write_audit(
                db,
                AuditEntry(
                    actor_id=actor.id if actor else None,
                    action="permission.denied",
                    resource_type="user",
                    outcome="failure",
                    reason_code=reason,
                ),
            )

    async def begin_login(self, return_path: str) -> LoginStart:
        validate_return_path(return_path)
        if (
            not self.settings.github_client_id
            or self.github is None
            or self.cipher is None
        ):
            raise secret_configuration_error()
        self.signing_key()
        state, nonce, verifier = random_token(), random_token(), random_token()
        async with self.factory.begin() as db:
            now = await db.scalar(select(func.clock_timestamp()))
            db.add(
                OAuthState(
                    state_hash=token_hash(state.get_secret_value()),
                    browser_nonce_hash=token_hash(nonce.get_secret_value()),
                    pkce_verifier_encrypted=self.cipher.encrypt(verifier),
                    return_path=return_path,
                    created_at=now,
                    expires_at=now + timedelta(minutes=10),
                )
            )
        challenge = (
            base64.urlsafe_b64encode(
                hashlib.sha256(verifier.get_secret_value().encode()).digest()
            )
            .rstrip(b"=")
            .decode()
        )
        query = urlencode(
            {
                "client_id": self.settings.github_client_id,
                "redirect_uri": self.settings.public_base_url.rstrip("/")
                + "/api/v1/auth/github/callback",
                "state": state.get_secret_value(),
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
        )
        return LoginStart(
            authorize_url="https://github.com/login/oauth/authorize?" + query,
            browser_nonce=nonce,
        )

    async def _consume_state(self, state: str, browser_nonce: str) -> OAuthState:
        reason = None
        async with self.factory.begin() as db:
            consumed = await db.scalar(
                update(OAuthState)
                .where(
                    OAuthState.state_hash == token_hash(state),
                    OAuthState.browser_nonce_hash == token_hash(browser_nonce),
                    OAuthState.consumed_at.is_(None),
                    OAuthState.expires_at > func.clock_timestamp(),
                )
                .values(consumed_at=func.clock_timestamp())
                .returning(OAuthState)
            )
            if consumed is None:
                row = await db.scalar(
                    select(OAuthState).where(OAuthState.state_hash == token_hash(state))
                )
                now = await db.scalar(select(func.clock_timestamp()))
                if row is None:
                    reason = "invalid_state"
                elif row.browser_nonce_hash != token_hash(browser_nonce):
                    reason = "browser_mismatch"
                elif row.consumed_at is not None:
                    reason = "state_consumed"
                elif row.expires_at <= now:
                    reason = "state_expired"
                else:
                    reason = "invalid_state"
                await write_audit(
                    db,
                    AuditEntry(
                        action="auth.login",
                        resource_type="session",
                        outcome="failure",
                        reason_code=reason,
                    ),
                )
        if reason:
            raise AppError("validation_error", 400, "Invalid login state")
        return consumed

    async def complete_login(
        self, code: str, state: str, browser_nonce: str
    ) -> SessionIssue:
        saved = await self._consume_state(state, browser_nonce)
        try:
            tokens = await self.github.exchange_code(
                SecretStr(code), self.cipher.decrypt(saved.pkce_verifier_encrypted)
            )
            profile = await self.github.get_user(tokens.access_token)
        except AppError as error:
            async with self.factory.begin() as db:
                await write_audit(
                    db,
                    AuditEntry(
                        action="auth.login",
                        resource_type="session",
                        outcome="failure",
                        reason_code=error.code,
                    ),
                )
            raise
        rejection = None
        async with self.factory.begin() as db:
            user = await db.scalar(
                select(User).where(User.github_id == profile.id).with_for_update()
            )
            if user is None:
                rejection = "member_not_allowed"
            elif not user.enabled:
                rejection = "member_disabled"
            elif saved.created_at <= user.auth_changed_at:
                rejection = "permission_revoked"
            if rejection:
                await write_audit(
                    db,
                    AuditEntry(
                        actor_id=user.id if user else None,
                        action="auth.login",
                        resource_type="session",
                        outcome="failure",
                        reason_code=rejection,
                    ),
                )
            else:
                # Shared lock order: User first, then its credential. Both tokens,
                # session and success audit commit together under this protection.
                credential = await db.scalar(
                    select(GitHubCredential)
                    .where(GitHubCredential.user_id == user.id)
                    .with_for_update()
                )
                if credential is None:
                    credential = GitHubCredential(user_id=user.id, version=1)
                    db.add(credential)
                else:
                    credential.version += 1
                credential.access_token_encrypted = self.cipher.encrypt(
                    tokens.access_token
                )
                credential.refresh_token_encrypted = (
                    self.cipher.encrypt(tokens.refresh_token)
                    if tokens.refresh_token
                    else None
                )
                credential.expires_at = tokens.expires_at
                credential.refresh_expires_at = tokens.refresh_expires_at
                credential.needs_reauth = False
                user.login, user.avatar_url = profile.login, profile.avatar_url
                raw = random_token()
                now = await db.scalar(select(func.clock_timestamp()))
                expires_at = now + timedelta(hours=8)
                session = Session(
                    user_id=user.id,
                    token_hash=token_hash(raw.get_secret_value()),
                    auth_version=user.auth_version,
                    created_at=now,
                    expires_at=expires_at,
                )
                db.add(session)
                await db.flush()
                await write_audit(
                    db,
                    AuditEntry(
                        actor_id=user.id,
                        action="auth.login",
                        resource_type="session",
                        resource_id=session.id,
                        outcome="success",
                    ),
                )
        if rejection:
            raise forbidden()
        return SessionIssue(
            raw_token=raw, expires_at=expires_at, return_path=saved.return_path
        )

    async def resolve_session(self, raw_token: str) -> Actor:
        async with self.factory() as db:
            user = await db.scalar(
                select(User)
                .join(Session, Session.user_id == User.id)
                .where(
                    Session.token_hash == token_hash(raw_token),
                    Session.expires_at > func.clock_timestamp(),
                    Session.auth_version == User.auth_version,
                    User.enabled,
                )
            )
            if user is None:
                raise unauthenticated()
            return Actor(
                id=user.id,
                github_id=user.github_id,
                role=user.role,
                auth_version=user.auth_version,
            )

    async def user_view(self, actor: Actor) -> UserView:
        async with self.factory() as db:
            user = await self._current_user(db, actor)
            return UserView.model_validate(user)

    async def logout(self, raw_token: str) -> None:
        async with self.factory.begin() as db:
            row = await db.scalar(
                delete(Session)
                .where(Session.token_hash == token_hash(raw_token))
                .returning(Session)
            )
            if row is not None:
                await write_audit(
                    db,
                    AuditEntry(
                        actor_id=row.user_id,
                        action="auth.logout",
                        resource_type="session",
                        resource_id=row.id,
                        outcome="success",
                    ),
                )

    async def _current_user(
        self, db: AsyncSession, actor: Actor, *, lock=False, admin=False
    ) -> User:
        query = select(User).where(User.id == actor.id)
        if lock:
            query = query.with_for_update()
        user = await db.scalar(query)
        if user is None or not user.enabled or user.auth_version != actor.auth_version:
            raise unauthenticated()
        if admin and user.role != "admin":
            raise forbidden()
        return user

    async def create_member(
        self, actor: Actor, github_id: int, role: Role = "member"
    ) -> UserView:
        conflict = False
        async with self.factory.begin() as db:
            await lock_admin_set(db)
            await self._current_user(db, actor, lock=True, admin=True)
            user = await db.scalar(
                insert(User)
                .values(github_id=github_id, role=role)
                .on_conflict_do_nothing(index_elements=[User.github_id])
                .returning(User)
            )
            conflict = user is None
            await write_audit(
                db,
                AuditEntry(
                    actor_id=actor.id,
                    action="member.created",
                    resource_type="user",
                    resource_id=user.id if user else None,
                    outcome="failure" if conflict else "success",
                    reason_code="idempotency_conflict" if conflict else None,
                ),
            )
            result = UserView.model_validate(user) if user else None
        if conflict:
            raise AppError("idempotency_conflict", 409, "Member already exists")
        return result

    async def update_member(
        self, actor: Actor, target_id: UUID, role: Role | None, enabled: bool | None
    ) -> UserView:
        error = None
        async with self.factory.begin() as db:
            await lock_admin_set(db)
            await self._current_user(db, actor, lock=True, admin=True)
            user = await db.scalar(
                select(User).where(User.id == target_id).with_for_update()
            )
            if user is None:
                error = AppError("resource_not_found", 404, "Resource not found")
            elif (
                user.enabled
                and user.role == "admin"
                and (role == "member" or enabled is False)
            ):
                count = await db.scalar(
                    select(func.count())
                    .select_from(User)
                    .where(User.enabled, User.role == "admin")
                )
                if count <= 1:
                    error = AppError(
                        "last_admin",
                        409,
                        "At least one active administrator is required",
                    )
            if error:
                await write_audit(
                    db,
                    AuditEntry(
                        actor_id=actor.id,
                        action="member.updated",
                        resource_type="user",
                        resource_id=target_id,
                        outcome="failure",
                        reason_code=error.code,
                    ),
                )
            else:
                changed = (role is not None and role != user.role) or (
                    enabled is not None and enabled != user.enabled
                )
                if changed:
                    if role is not None:
                        user.role = role
                    if enabled is not None:
                        user.enabled = enabled
                    user.auth_version += 1
                    user.auth_changed_at = await db.scalar(
                        select(func.clock_timestamp())
                    )
                    await db.execute(delete(Session).where(Session.user_id == user.id))
                    if not user.enabled:
                        credential = await db.scalar(
                            select(GitHubCredential)
                            .where(GitHubCredential.user_id == user.id)
                            .with_for_update()
                        )
                        if credential:
                            credential.access_token_encrypted = (
                                credential.refresh_token_encrypted
                            ) = None
                            credential.expires_at = credential.refresh_expires_at = None
                            credential.needs_reauth = True
                            credential.version += 1
                await write_audit(
                    db,
                    AuditEntry(
                        actor_id=actor.id,
                        action="member.updated",
                        resource_type="user",
                        resource_id=user.id,
                        outcome="success",
                    ),
                )
                result = UserView.model_validate(user)
        if error:
            raise error
        return result

    async def _list(
        self, actor: Actor, model, view, purpose: str, limit: int, cursor: str | None
    ):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise AppError("validation_error", 400, "Invalid page limit")
        codec = CursorCodec(self.signing_key())
        query = select(model)
        if cursor is not None:
            position = codec.decode_cursor(cursor, actor.id, purpose, limit)
            try:
                timestamp = datetime.fromisoformat(position["created_at"])
                row_id = UUID(position["id"])
                if timestamp.tzinfo is None:
                    raise ValueError
            except (TypeError, ValueError, KeyError):
                raise AppError("invalid_cursor", 400, "Invalid cursor") from None
            query = query.where(
                tuple_(model.created_at, model.id) < tuple_(timestamp, row_id)
            )
        async with self.factory() as db:
            await self._current_user(db, actor, admin=True)
            rows = list(
                await db.scalars(
                    query.order_by(model.created_at.desc(), model.id.desc()).limit(
                        limit + 1
                    )
                )
            )
            next_cursor = None
            if len(rows) > limit:
                last = rows[limit - 1]
                next_cursor = codec.encode_cursor(
                    actor.id,
                    purpose,
                    {"created_at": last.created_at.isoformat(), "id": str(last.id)},
                    limit,
                )
            return Page[view](
                items=[view.model_validate(row) for row in rows[:limit]],
                next_cursor=next_cursor,
            )

    async def list_members(
        self, actor: Actor, limit: int = 20, cursor: str | None = None
    ) -> Page[UserView]:
        return await self._list(actor, User, UserView, "admin.users", limit, cursor)

    async def list_audit(
        self, actor: Actor, limit: int = 20, cursor: str | None = None
    ) -> Page[AuditView]:
        return await self._list(
            actor, AuditLog, AuditView, "admin.audit", limit, cursor
        )
