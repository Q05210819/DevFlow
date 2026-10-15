"""Authenticated lifecycle deliveries; receipt, revocation and audit commit together."""

import hashlib
import hmac
import json
from dataclasses import dataclass
from uuid import UUID

from fastapi import APIRouter, Request, Response
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from devflow.audit.service import AuditEntry, write_audit
from devflow.auth.models import AUTH_ORDER_SEQUENCE, Session, User
from devflow.db import lock_admin_set
from devflow.errors import AppError
from devflow.github.models import GitHubCredential, GitHubInstallation, WebhookDelivery
from devflow.repositories.models import Repository
from devflow.sync.scheduler import request_sync
from devflow.sync.schemas import SyncTarget
from devflow.sync.scope import authorized

router = APIRouter(prefix="/api/v1/github", tags=["github"])


def unavailable() -> AppError:
    return AppError("service_unavailable", 503, "Service unavailable")


@dataclass(frozen=True)
class Lifecycle:
    event_name: str
    action: str | None = None
    installation_id: int | None = None
    github_user_id: int | None = None
    added: tuple[int, ...] = ()
    removed: tuple[int, ...] = ()


@dataclass(frozen=True)
class ContentHint:
    event_name: str
    action: str | None
    installation_id: int
    github_repository_id: int
    target: SyncTarget | None = None


CONTENT_ACTIONS = {
    "issues": {
        "opened",
        "edited",
        "deleted",
        "closed",
        "reopened",
        "assigned",
        "unassigned",
        "labeled",
        "unlabeled",
        "locked",
        "unlocked",
        "milestoned",
        "demilestoned",
        "pinned",
        "unpinned",
        "transferred",
        "typed",
        "untyped",
    },
    "issue_comment": {"created", "edited", "deleted"},
    "pull_request": {
        "opened",
        "edited",
        "closed",
        "reopened",
        "synchronize",
        "assigned",
        "unassigned",
        "labeled",
        "unlabeled",
        "locked",
        "unlocked",
        "milestoned",
        "demilestoned",
        "converted_to_draft",
        "ready_for_review",
        "review_requested",
        "review_request_removed",
        "enqueued",
        "dequeued",
        "auto_merge_enabled",
        "auto_merge_disabled",
    },
    "pull_request_review": {"submitted", "edited", "dismissed"},
    "pull_request_review_comment": {"created", "edited", "deleted"},
    "workflow_run": {"requested", "in_progress", "completed"},
    "workflow_job": {"queued", "in_progress", "completed", "waiting"},
    "push": {None},
    "create": {None},
    "delete": {None},
    "repository": {
        "renamed",
        "transferred",
        "deleted",
        "privatized",
        "publicized",
        "archived",
        "unarchived",
        "edited",
    },
}


def parse_event(raw_body: bytes, event_name: str) -> Lifecycle | ContentHint:
    if event_name not in CONTENT_ACTIONS:
        return parse_lifecycle(raw_body, event_name)
    data = json.loads(raw_body)
    if not isinstance(data, dict):
        raise TypeError
    action = data.get("action")
    if action is not None and not isinstance(action, str):
        raise TypeError
    if action not in CONTENT_ACTIONS[event_name]:
        return Lifecycle(event_name)
    installation_id = numeric_id(data.get("installation"))
    repository_id = numeric_id(data.get("repository"))
    target = None
    if event_name in {"issues", "issue_comment"}:
        issue = data.get("issue")
        source_id = numeric_id(issue)
        number = numeric_id({"id": issue.get("number")})
        pull = "pull_request" in issue
        target = SyncTarget(
            kind="pull" if pull else "issue",
            source_id=None if pull else source_id,
            number=number,
        )
    elif event_name.startswith("pull_request"):
        pull = data.get("pull_request")
        target = SyncTarget(
            kind="pull",
            source_id=numeric_id(pull),
            number=numeric_id({"id": pull.get("number")}),
        )
    elif event_name in {"workflow_run", "workflow_job"}:
        item = data.get(event_name)
        source_id = numeric_id(
            item
            if event_name == "workflow_run"
            else {"id": item.get("run_id")}
            if isinstance(item, dict)
            else None
        )
        attempt = item.get("run_attempt")
        if attempt is not None:
            attempt = numeric_id({"id": attempt})
        target = SyncTarget(kind="ci_run", source_id=source_id, attempt=attempt)
    return ContentHint(event_name, action, installation_id, repository_id, target)


def numeric_id(value: object) -> int:
    if not isinstance(value, dict):
        raise TypeError
    identity = value.get("id")
    if type(identity) is not int or not 0 < identity < 2**63:
        raise ValueError
    return identity


def parse_lifecycle(raw_body: bytes, event_name: str) -> Lifecycle:
    data = json.loads(raw_body)
    if not isinstance(data, dict):
        raise TypeError
    action = data.get("action")
    allowed = {
        "installation": (
            "created",
            "deleted",
            "suspend",
            "unsuspend",
            "new_permissions_accepted",
        ),
        "installation_repositories": ("added", "removed"),
        "github_app_authorization": ("revoked",),
    }
    if event_name not in allowed:
        return Lifecycle("unknown")
    if action not in allowed[event_name]:
        return Lifecycle(event_name)
    if event_name == "github_app_authorization":
        return Lifecycle(
            event_name, action, github_user_id=numeric_id(data.get("sender"))
        )
    installation_id = numeric_id(data.get("installation"))
    if event_name == "installation":
        return Lifecycle(event_name, action, installation_id)
    added, removed = (
        data.get("repositories_added", []),
        data.get("repositories_removed", []),
    )
    if not isinstance(added, list) or not isinstance(removed, list):
        raise TypeError
    return Lifecycle(
        event_name,
        action,
        installation_id,
        added=tuple(numeric_id(item) for item in added),
        removed=tuple(numeric_id(item) for item in removed),
    )


class WebhookService:
    def __init__(self, factory: async_sessionmaker[AsyncSession], secret: bytes):
        self.factory, self.secret = factory, secret

    async def handle_webhook(
        self, raw_body: bytes, signature: str, delivery_id: str, event_name: str
    ) -> None:
        if not self.secret:
            raise unavailable()
        expected = (
            "sha256=" + hmac.new(self.secret, raw_body, hashlib.sha256).hexdigest()
        )
        if not hmac.compare_digest(expected.encode("ascii"), signature.encode("utf-8")):
            try:
                async with self.factory.begin() as db:
                    await write_audit(
                        db,
                        AuditEntry(
                            action="webhook.verified",
                            resource_type="webhook",
                            outcome="failure",
                            reason_code="invalid_signature",
                        ),
                    )
            except SQLAlchemyError:
                raise unavailable() from None
            raise AppError("unauthenticated", 401, "Invalid webhook signature")
        try:
            delivery_uuid = UUID(delivery_id)
            lifecycle = parse_event(raw_body, event_name)
        except (ValueError, TypeError, UnicodeError, RecursionError):
            raise AppError("validation_error", 400, "Invalid webhook payload") from None
        try:
            await self._process(delivery_uuid, lifecycle)
        except SQLAlchemyError:
            raise unavailable() from None

    async def _process(self, delivery_id: UUID, event: Lifecycle | ContentHint) -> None:
        async with self.factory.begin() as db:
            content = isinstance(event, ContentHint)
            target = event.target if content else None
            receipt = await db.scalar(
                insert(WebhookDelivery)
                .values(
                    delivery_id=delivery_id,
                    event_name=event.event_name,
                    action=event.action,
                    installation_id=event.installation_id,
                    github_user_id=None if content else event.github_user_id,
                    github_repository_id=event.github_repository_id
                    if content
                    else None,
                    target_kind=target.kind if target else None,
                    source_id=target.source_id if target else None,
                    number=target.number if target else None,
                    attempt=target.attempt if target else None,
                    status="processed" if content or event.action else "ignored",
                )
                .on_conflict_do_nothing(index_elements=["delivery_id"])
                .returning(WebhookDelivery)
            )
            if receipt is None:
                return
            if content:
                if not await self._content(db, event):
                    receipt.status = "ignored"
            elif event.installation_id is not None:
                await self._installation(db, event)
            if not content and event.github_user_id is not None:
                await self._user(db, event.github_user_id)
            await write_audit(
                db,
                AuditEntry(
                    action="webhook.processed",
                    resource_type="webhook",
                    resource_id=receipt.id,
                    outcome="success",
                ),
            )

    async def _content(self, db: AsyncSession, event: ContentHint) -> bool:
        # Content never upserts installations or changes their authorization epoch.
        installation = await db.scalar(
            select(GitHubInstallation)
            .where(GitHubInstallation.installation_id == event.installation_id)
            .with_for_update()
        )
        if installation is None:
            return False
        repository = await db.scalar(
            select(Repository)
            .where(
                Repository.github_repository_id == event.github_repository_id,
                Repository.installation_id == event.installation_id,
            )
            .with_for_update()
        )
        if repository is None:
            return False
        if event.event_name == "repository":
            if not repository.available and not repository.needs_verification:
                return False
            repository.auth_version += 1
            repository.verified_at = None
            repository.needs_verification = event.action != "deleted"
            if event.action == "deleted":
                repository.available = False
            await write_audit(
                db,
                AuditEntry(
                    action="repository.invalidated",
                    resource_type="repository",
                    resource_id=repository.id,
                    outcome="success",
                ),
            )
            return True
        if not authorized(installation, repository, None):
            return False
        state = await request_sync(
            db, repository.id, reason="webhook", target=event.target
        )
        if event.event_name in {"push", "create", "delete"}:
            state.version_hint_sequence += 1
        return True

    async def _installation(self, db: AsyncSession, event: Lifecycle) -> None:
        # Upsert even unknown IDs: a missing row must not let first registration
        # race past a processed deletion. Conflict waits serialize that race.
        await db.execute(
            insert(GitHubInstallation)
            .values(installation_id=event.installation_id)
            .on_conflict_do_nothing(index_elements=["installation_id"])
        )
        installation = await db.scalar(
            select(GitHubInstallation)
            .where(GitHubInstallation.installation_id == event.installation_id)
            .with_for_update()
        )
        installation.auth_version += 1
        all_repositories = event.event_name == "installation"
        revoked = event.action in ("deleted", "suspend")
        if all_repositories:
            installation.needs_verification = not revoked
            if revoked:
                installation.available = False
            installation.verified_at = None

        # GitHub's all→selected change may carry an empty removal array.
        # Its full scope is unknown, so require verification of every stored row.
        uncertain_removal = event.action == "removed" and not event.removed
        query = select(Repository).where(
            Repository.installation_id == event.installation_id
        )
        if not all_repositories and not uncertain_removal:
            query = query.where(
                Repository.github_repository_id.in_(event.added + event.removed)
            )
        repositories = await db.scalars(query.order_by(Repository.id).with_for_update())
        for repository in repositories:
            remove = revoked or repository.github_repository_id in event.removed
            repository.auth_version += 1
            repository.needs_verification = not remove
            repository.verified_at = None
            if remove:
                repository.available = False
            await write_audit(
                db,
                AuditEntry(
                    action="repository.invalidated",
                    resource_type="repository",
                    resource_id=repository.id,
                    outcome="success",
                    reason_code="permission_revoked" if remove else None,
                ),
            )

    async def _user(self, db: AsyncSession, github_user_id: int) -> None:
        # A row lock cannot see an uncommitted member insertion. Serialize with
        # create_member/bootstrap before deciding this identity is unknown.
        await lock_admin_set(db)
        # Credential refresh and OAuth callback share this lock order. A refresh
        # already in flight finishes first; this transaction then clears its pair.
        user = await db.scalar(
            select(User).where(User.github_id == github_user_id).with_for_update()
        )
        if user is None:
            return
        credential = await db.scalar(
            select(GitHubCredential)
            .where(GitHubCredential.user_id == user.id)
            .with_for_update()
        )
        user.auth_version += 1
        user.auth_changed_at = func.clock_timestamp()
        user.auth_changed_order = AUTH_ORDER_SEQUENCE.next_value()
        if credential is not None:
            credential.access_token_encrypted = credential.refresh_token_encrypted = (
                None
            )
            credential.expires_at = credential.refresh_expires_at = None
            credential.needs_reauth = True
            credential.version += 1
        await db.execute(delete(Session).where(Session.user_id == user.id))
        await write_audit(
            db,
            AuditEntry(
                actor_id=user.id,
                action="credential.revoked",
                resource_type="user",
                resource_id=user.id,
                outcome="success",
                reason_code="permission_revoked",
            ),
        )


@router.post("/webhooks", status_code=204)
async def github_webhook(request: Request) -> Response:
    service = request.app.state.webhook_service
    if service is None:
        raise unavailable()
    await service.handle_webhook(
        await request.body(),
        request.headers.get("X-Hub-Signature-256", ""),
        request.headers.get("X-GitHub-Delivery", ""),
        request.headers.get("X-GitHub-Event", ""),
    )
    return Response(status_code=204)
