"""Repository discovery and idempotent onboarding using immutable GitHub IDs."""

import hashlib
import json
from datetime import datetime
from uuid import UUID

from sqlalchemy import select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from devflow.audit.service import AuditEntry, write_audit
from devflow.auth.service import forbidden
from devflow.auth.types import Actor
from devflow.config import Settings
from devflow.errors import AppError
from devflow.github.client import GitHubClient, not_found
from devflow.github.credentials import CredentialService
from devflow.github.models import GitHubInstallation
from devflow.pagination import CursorCodec, Page
from devflow.repositories.models import Repository
from devflow.repositories.policy import RepositoryPolicy, check_actor
from devflow.repositories.schemas import CandidateRepository, RepositoryView
from devflow.security import read_secret_file, secret_configuration_error
from devflow.sync.models import RepositoryCommandReceipt
from devflow.sync.scheduler import request_sync


def invalid_cursor():
    return AppError("invalid_cursor", 400, "Invalid cursor")


async def command_receipt(
    db: AsyncSession,
    *,
    actor_id: UUID,
    repository_id: UUID,
    operation: str,
    idempotency_key: str,
    normalized_payload: dict,
) -> tuple[RepositoryCommandReceipt | None, str]:
    """Caller holds User -> Installation -> Repository; replay still needs access.

    Returns the durable receipt and canonical hash. The caller records the demand
    reference and audit in this transaction and renders the current resource.
    """
    payload_hash = hashlib.sha256(
        json.dumps(normalized_payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    receipt = await db.get(
        RepositoryCommandReceipt, (actor_id, repository_id, operation, idempotency_key)
    )
    if receipt and receipt.payload_hash != payload_hash:
        raise AppError(
            "idempotency_conflict", 409, "Idempotency key conflicts with input"
        )
    return receipt, payload_hash


class RepositoryService:
    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        github: GitHubClient,
        credentials: CredentialService,
        settings: Settings,
        *,
        policy: RepositoryPolicy | None = None,
    ):
        self.factory, self.github, self.credentials, self.settings = (
            factory,
            github,
            credentials,
            settings,
        )
        self.policy = policy or RepositoryPolicy(factory, github, credentials, settings)

    def codec(self):
        key = read_secret_file(self.settings.app_signing_key_file)
        if len(key) < 32:
            raise secret_configuration_error()
        return CursorCodec(key)

    def configuration(self):
        return hashlib.sha256(
            json.dumps(
                [
                    self.settings.github_app_id,
                    sorted(self.settings.github_allowed_installation_ids),
                ]
            ).encode()
        ).hexdigest()

    def decode(self, actor, limit, cursor, purpose):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise invalid_cursor()
        if cursor is None:
            return None
        value = self.codec().decode_cursor(cursor, actor.id, purpose, limit)
        if value.get("configuration") != self.configuration():
            raise invalid_cursor()
        return value

    def encode(self, actor, limit, position, purpose):
        return self.codec().encode_cursor(
            actor.id,
            purpose,
            {**position, "configuration": self.configuration()},
            limit,
        )

    async def discover(
        self, actor: Actor, limit: int = 20, cursor: str | None = None
    ) -> Page[CandidateRepository]:
        purpose = "repository-discovery"
        position = self.decode(actor, limit, cursor, purpose)
        ip, ii, rp, ri, expected = 1, 0, 1, 0, None
        if position:
            try:
                ip, ii, rp, ri, expected = [
                    position[key]
                    for key in (
                        "installation_page",
                        "installation_offset",
                        "repository_page",
                        "repository_offset",
                        "installation_id",
                    )
                ]
                if any(
                    type(v) is not int or v < minimum
                    for v, minimum in (
                        (ip, 1),
                        (ii, 0),
                        (rp, 1),
                        (ri, 0),
                        (expected, 1),
                    )
                ):
                    raise ValueError
            except (KeyError, ValueError, TypeError):
                raise invalid_cursor() from None
        token = await self.credentials.get_user_token(actor)
        items = []
        epochs = {}
        while ip is not None:
            installations = await self.policy.user_request(
                actor, token, self.github.list_installations, ip
            )
            if expected is not None and (
                ii >= len(installations.items) or installations.items[ii].id != expected
            ):
                raise invalid_cursor()
            for index in range(ii, len(installations.items)):
                item = installations.items[index]
                if (
                    not self.policy.allowed(item.id)
                    or item.app_id != self.settings.github_app_id
                    or item.suspended_at is not None
                ):
                    rp, ri = 1, 0
                    continue
                try:
                    epoch = await self.policy.installation_snapshot(actor, item.id)
                    await self.github.get_installation(item.id)
                    while rp is not None:
                        repositories = await self.policy.user_request(
                            actor, token, self.github.list_repositories, item.id, rp
                        )
                        for offset in range(ri, len(repositories.items)):
                            candidate = repositories.items[offset]
                            async with self.factory.begin() as session:
                                await check_actor(session, actor, lock=True)
                                installation = await session.scalar(
                                    select(GitHubInstallation)
                                    .where(
                                        GitHubInstallation.installation_id == item.id
                                    )
                                    .with_for_update()
                                )
                                if (
                                    installation.auth_version != epoch
                                    or not self.policy.allowed(item.id)
                                ):
                                    raise not_found()
                                existing = await session.scalar(
                                    select(Repository).where(
                                        Repository.github_repository_id == candidate.id
                                    )
                                )
                                if existing and (
                                    not existing.enabled
                                    or (
                                        not existing.available
                                        and not existing.needs_verification
                                    )
                                ):
                                    continue
                            if len(items) == limit:
                                following = {
                                    "installation_page": ip,
                                    "installation_offset": index,
                                    "repository_page": rp,
                                    "repository_offset": offset,
                                    "installation_id": item.id,
                                }
                                return Page(
                                    items=await self.filter_candidates(
                                        actor, items, epochs
                                    ),
                                    next_cursor=self.encode(
                                        actor, limit, following, purpose
                                    ),
                                )
                            items.append(
                                CandidateRepository(
                                    github_repository_id=candidate.id,
                                    installation_id=item.id,
                                    full_name=candidate.full_name,
                                    private=candidate.private,
                                    default_branch=candidate.default_branch,
                                )
                            )
                            epochs[(item.id, candidate.id)] = (
                                epoch,
                                existing.auth_version if existing else None,
                            )
                        rp, ri = repositories.next_page, 0
                except AppError as error:
                    if error.status != 404:
                        raise
                rp, ri = 1, 0
            ip, ii, expected = installations.next_page, 0, None
        return Page(items=await self.filter_candidates(actor, items, epochs))

    async def filter_candidates(self, actor, items, epochs):
        visible = []
        async with self.factory() as session:
            await check_actor(session, actor)
            for item in items:
                installation = await session.scalar(
                    select(GitHubInstallation).where(
                        GitHubInstallation.installation_id == item.installation_id
                    )
                )
                existing = await session.scalar(
                    select(Repository).where(
                        Repository.github_repository_id == item.github_repository_id
                    )
                )
                installation_epoch, repository_epoch = epochs[
                    (item.installation_id, item.github_repository_id)
                ]
                if (
                    not installation
                    or installation.auth_version != installation_epoch
                    or (
                        not installation.available
                        and not installation.needs_verification
                    )
                    or not self.policy.allowed(item.installation_id)
                ):
                    continue
                if existing and (
                    not existing.enabled
                    or (not existing.available and not existing.needs_verification)
                    or existing.auth_version != (repository_epoch or 1)
                ):
                    continue
                if existing is None and repository_epoch is not None:
                    continue
                visible.append(item)
        return visible

    async def register(
        self, actor: Actor, installation_id: int, github_repository_id: int
    ) -> RepositoryView:
        view, _ = await self.register_result(
            actor, installation_id, github_repository_id
        )
        return view

    async def register_result(
        self, actor: Actor, installation_id: int, github_repository_id: int
    ) -> tuple[RepositoryView, bool]:
        try:
            return await self._register(actor, installation_id, github_repository_id)
        except AppError as error:
            await self.policy.audit_denial(actor, error)
            raise

    async def _register(
        self, actor: Actor, installation_id: int, github_repository_id: int
    ) -> tuple[RepositoryView, bool]:
        if any(
            type(number) is not int or not 0 < number <= 9223372036854775807
            for number in (installation_id, github_repository_id)
        ):
            raise AppError("validation_error", 400, "Invalid repository identifier")
        async with self.factory() as session:
            user = await check_actor(session, actor)
            if user.role != "admin":
                raise forbidden()
        proof = await self.policy.verify(actor, installation_id, github_repository_id)
        async with self.factory.begin() as session:
            user, installation, repository = await self.policy.lock_verified(
                session, actor, proof
            )
            if user.role != "admin":
                raise forbidden()
            created = repository is None
            changed = created or (
                not repository.available
                or repository.needs_verification
                or not installation.available
                or installation.needs_verification
                or repository.installation_id != proof.installation.id
                or repository.full_name != proof.repository.full_name
                or repository.private != proof.repository.private
                or repository.default_branch != proof.repository.default_branch
            )
            if created:
                repository = Repository(github_repository_id=github_repository_id)
                session.add(repository)
            self.policy.promote(installation, repository, proof)
            await session.flush()
            if changed:
                await request_sync(
                    session, repository.id, reason="registration", actor_id=actor.id
                )
                await write_audit(
                    session,
                    AuditEntry(
                        actor_id=actor.id,
                        action="repository.registered",
                        resource_type="repository",
                        resource_id=repository.id,
                        outcome="success",
                    ),
                )
            view = RepositoryView.model_validate(repository)
        return view, created

    async def list_enabled(
        self, actor: Actor, limit: int = 20, cursor: str | None = None
    ) -> Page[RepositoryView]:
        purpose = "repository-list"
        position = self.decode(actor, limit, cursor, purpose)
        after = None
        if position:
            try:
                after = (
                    datetime.fromisoformat(position["created_at"]),
                    UUID(position["id"]),
                )
                if after[0].tzinfo is None:
                    raise ValueError
            except (ValueError, KeyError, TypeError):
                raise invalid_cursor() from None
        async with self.factory() as session:
            await check_actor(session, actor)
        items, last_visible = [], None
        while True:
            async with self.factory() as session:
                query = (
                    select(Repository)
                    .where(Repository.enabled.is_(True))
                    .order_by(Repository.created_at.desc(), Repository.id.desc())
                    .limit(100)
                )
                if after:
                    query = query.where(
                        tuple_(Repository.created_at, Repository.id) < after
                    )
                rows = list(await session.scalars(query))
            if not rows:
                return Page(items=await self.filter_enabled(actor, items))
            for row in rows:
                after = (row.created_at, row.id)
                try:
                    view = await self.policy.require(actor, row.id)
                except AppError as error:
                    if error.status == 404:
                        continue
                    raise
                if len(items) == limit:
                    return Page(
                        items=await self.filter_enabled(actor, items),
                        next_cursor=self.encode(actor, limit, last_visible, purpose),
                    )
                items.append(view)
                last_visible = {
                    "created_at": row.created_at.isoformat(),
                    "id": str(row.id),
                }

    async def filter_enabled(self, actor, items):
        from devflow.facts.service import recheck
        from devflow.jobs.models import Job
        from devflow.jobs.service import JobService
        from devflow.repositories.schemas import (
            RepositoryListView,
            RepositorySyncSummary,
        )
        from devflow.sync.models import RepositorySync

        authorized = []
        for item in items:
            try:
                key = await JobService(self.factory, self.policy)._authorize(
                    actor, item.id, fresh=False
                )
                authorized.append((item, key))
            except AppError as error:
                if error.status != 404:
                    raise
        async with self.factory() as db:
            summaries = {
                state.repository_id: RepositorySyncSummary(
                    status=state.status,
                    generation=state.generation,
                    requested_sequence=state.requested_sequence,
                    processed_sequence=state.processed_sequence,
                    error_code="budget_exhausted"
                    if state.status == "budget_exhausted"
                    else job.error_code
                    if job
                    else None,
                    retry_at=job.available_at
                    if job and job.status == "retry_wait"
                    else None,
                )
                for state, job in await db.execute(
                    select(RepositorySync, Job)
                    .outerjoin(Job, Job.id == RepositorySync.active_job_id)
                    .where(
                        RepositorySync.repository_id.in_(
                            [item.id for item, _ in authorized]
                        )
                    )
                )
            }
        visible = []
        for item, key in authorized:
            try:
                await recheck(self.policy, actor, item.id, key)
                visible.append(
                    RepositoryListView(
                        **item.model_dump(),
                        sync=summaries.get(item.id, RepositorySyncSummary()),
                    )
                )
            except AppError as error:
                if error.status != 404:
                    raise
        return visible

    async def detail(self, actor, repository_id):
        from devflow.facts.service import recheck
        from devflow.jobs.service import JobService
        from devflow.repositories.schemas import RepositoryDetailView
        from devflow.sync.routes import sync_view
        from devflow.sync.scope import transaction

        jobs = JobService(self.factory, self.policy)
        key = await jobs._authorize(actor, repository_id, fresh=False)
        try:
            async with transaction(self.factory) as db:
                await jobs._lock_authorization(db, actor, key)
                repo = await db.get(Repository, repository_id)
                result = RepositoryDetailView(
                    **RepositoryView.model_validate(repo).model_dump(),
                    sync=await sync_view(db, repository_id),
                )
        except AppError as error:
            await self.policy.audit_denial(actor, error, repository_id)
            raise
        await recheck(self.policy, actor, repository_id, key)
        return result
