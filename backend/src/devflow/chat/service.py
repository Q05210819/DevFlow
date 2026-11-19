"""Creator-private conversations and their single durable active run.

A conversation freezes one explicit source set at creation time. Every read,
submission, Job lookup and SSE replay re-authorizes *all* participating
repositories, so an indexless fork that only takes part in retrieval still
revokes the whole conversation when its grant disappears. Answer execution is
deliberately absent here: the model worker owns it in later tasks.
"""

import hashlib
import json
from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID, uuid4

from pydantic import TypeAdapter, ValidationError
from sqlalchemy import Text, func, select, text
from sqlalchemy import delete as sa_delete
from sqlalchemy.dialects.postgresql import insert

from devflow.audit.service import AuditEntry, write_audit
from devflow.auth.types import Actor
from devflow.chat.models import (
    AgentRun,
    Conversation,
    ConversationRepository,
    Message,
    ToolCall,
)
from devflow.chat.schemas import (
    CitationView,
    ConversationList,
    ConversationSummary,
    ConversationView,
    DeletionReceipt,
    MessagePage,
    MessageView,
    RunOutcome,
    RunReceipt,
    RunSummary,
)
from devflow.errors import AppError
from devflow.github.client import not_found
from devflow.jobs.models import Job
from devflow.jobs.queue import Queue
from devflow.jobs.schemas import IdempotencyKey, JobErrorCode
from devflow.jobs.service import append_event
from devflow.knowledge.access import KnowledgeAccess
from devflow.knowledge.schemas import EvidenceManifest, QuerySelection
from devflow.sync.scope import transaction

_KEY = TypeAdapter(IdempotencyKey)
_ERROR = TypeAdapter(JobErrorCode)
_KIND = "chat.run"
_CLEANUP_KIND = "chat.cleanup"
_GRAPH_VERSION = "chat-graph-v1"
MAX_REPOSITORIES = 3
MAX_CONVERSATIONS = 100
MAX_MESSAGES = 200
MAX_QUESTION_BYTES = 2048
MESSAGE_CAPACITY = 256 * 1024 * 1024
CLEANUP_LIMIT = 100
_MESSAGE_LOCK = "devflow:chat:messages"
QUEUE_DEADLINE = timedelta(minutes=10)
DEFAULT_HISTORY_LIMIT = 30
MAX_HISTORY_LIMIT = 100


def _storage_exhausted():
    return AppError("storage_exhausted", 409, "Conversation storage exhausted")


def _invalid():
    return AppError("validation_error", 422, "Invalid conversation request")


def _conflict():
    return AppError("idempotency_conflict", 409, "Idempotency key conflicts with input")


def _scope_mismatch():
    return AppError("scope_mismatch", 409, "Selection does not match conversation")


def _run_in_progress():
    return AppError("run_in_progress", 409, "Conversation already has an active run")


def _archived():
    return AppError("conversation_archived", 409, "Conversation is archived")


def _lease_expired():
    return AppError("lease_expired", 409, "Run lease expired")


def _answer_invalid():
    return AppError("answer_validation_failed", 422, "Answer record is invalid")


@dataclass(frozen=True, slots=True)
class JobFence:
    """The exact Job state a controller observed before it may retire a run."""

    status: str
    owner: UUID | None
    generation: int
    attempt: int


class ChatService:
    """Private conversation metadata; never a model, tool or retrieval executor."""

    def __init__(self, factory, policy):
        self.factory, self.policy = factory, policy
        self.access = KnowledgeAccess(policy)
        self.max_attempts = 3

    async def create(
        self, actor: Actor, selected_repository_ids, key: str
    ) -> ConversationView:
        try:
            key = _KEY.validate_python(key)
            repositories = self._repositories(selected_repository_ids)
        except (ValidationError, TypeError, ValueError):
            raise _invalid() from None
        input_hash = self._digest(
            {"selected_repository_ids": sorted(str(item) for item in repositories)}
        )
        grant = await self.access.verify(actor, repositories, fresh=True)
        async with transaction(self.factory) as db:
            await self.access.lock(db, grant)
            now = await db.scalar(select(func.clock_timestamp()))
            conversation = await db.scalar(
                insert(Conversation)
                .values(
                    id=uuid4(),
                    actor_id=actor.id,
                    idempotency_key=key,
                    input_hash=input_hash,
                    created_at=now,
                    updated_at=now,
                )
                .on_conflict_do_nothing(constraint="uq_chat_conversations_actor_key")
                .returning(Conversation)
            )
            if conversation is None:
                return await self._replay_create(db, actor, key, input_hash)
            # Insert-then-count counts archived rows and is serialized by the
            # actor row lock above; a rejected 101st row rolls back with it.
            # Archiving never bypasses the durable per-user bound.
            count = await db.scalar(
                select(func.count())
                .select_from(Conversation)
                .where(Conversation.actor_id == actor.id)
            )
            if count > MAX_CONVERSATIONS:
                raise _storage_exhausted()
            for ordinal, repository_id in enumerate(repositories):
                db.add(
                    ConversationRepository(
                        conversation_id=conversation.id,
                        repository_id=repository_id,
                        ordinal=ordinal,
                    )
                )
            await db.flush()
            return ConversationView(
                id=conversation.id,
                selected_repository_ids=repositories,
                archived=False,
                created_at=conversation.created_at,
            )

    async def _replay_create(self, db, actor, key, input_hash):
        conversation = await db.scalar(
            select(Conversation).where(
                Conversation.actor_id == actor.id,
                Conversation.idempotency_key == key,
            )
        )
        if conversation is None or conversation.input_hash != input_hash:
            # Validation and denial auditing belong to the caller's taxonomy;
            # a replay never writes a second conversation or Job.
            raise _conflict()
        return ConversationView(
            id=conversation.id,
            selected_repository_ids=await self._members(db, conversation.id),
            archived=conversation.archived_at is not None,
            created_at=conversation.created_at,
        )

    async def submit(
        self, actor: Actor, conversation_id: UUID, selection, question: str, key: str
    ) -> RunReceipt:
        try:
            key = _KEY.validate_python(key)
            chosen = QuerySelection.model_validate(selection)
        except (ValidationError, TypeError, ValueError):
            raise _invalid() from None
        if (
            not isinstance(conversation_id, UUID)
            or not isinstance(question, str)
            or not question.strip()
            or len(question.encode("utf-8")) > MAX_QUESTION_BYTES
        ):
            raise _invalid()
        question_bytes = len(question.encode("utf-8"))
        async with self.factory() as db:
            conversation = await db.get(Conversation, conversation_id)
            if conversation is None or conversation.actor_id != actor.id:
                raise not_found()
            if conversation.archived_at is not None:
                raise _archived()
            members = await self._members(db, conversation_id)
        if set(chosen.selected_repository_ids) != set(members):
            raise _scope_mismatch()
        canonical = QuerySelection(
            selected_repository_ids=members,
            indexes=tuple(
                sorted(chosen.indexes, key=lambda item: str(item.repository_id))
            ),
        )
        chosen_data = canonical.model_dump(mode="json")
        input_hash = self._digest(
            {
                "conversation_id": str(conversation_id),
                "question": question,
                "selection": chosen_data,
            }
        )
        grant = await self.access.verify(actor, members, fresh=True)
        try:
            async with transaction(self.factory) as db:
                await self.access.lock(db, grant)
                # Serializes submissions per conversation, including the replay
                # probe and the single-active-run check that follows.
                conversation = await db.scalar(
                    select(Conversation)
                    .where(
                        Conversation.id == conversation_id,
                        Conversation.actor_id == actor.id,
                    )
                    .with_for_update()
                )
                if conversation is None:
                    raise not_found()
                if conversation.archived_at is not None:
                    raise _archived()
                if await self._members(db, conversation_id) != members:
                    raise _scope_mismatch()
                now = await db.scalar(select(func.clock_timestamp()))
                run_id, message_id = uuid4(), uuid4()
                job = await db.scalar(
                    insert(Job)
                    .values(
                        id=uuid4(),
                        kind=_KIND,
                        repository_id=members[0],
                        requested_by=actor.id,
                        input={
                            "version": 1,
                            "run_id": str(run_id),
                            "conversation_id": str(conversation_id),
                            "message_id": str(message_id),
                        },
                        input_hash=input_hash,
                        idempotency_key=key,
                        max_attempts=self.max_attempts,
                    )
                    .on_conflict_do_nothing(constraint="uq_jobs_creator_kind_key")
                    .returning(Job)
                )
                if job is None:
                    return await self._replay_submit(db, actor, key, input_hash)
                active = await db.scalar(
                    select(AgentRun.id).where(
                        AgentRun.conversation_id == conversation_id,
                        AgentRun.terminal_at.is_(None),
                    )
                )
                if active is not None:
                    raise _run_in_progress()
                count = await db.scalar(
                    select(func.count())
                    .select_from(Message)
                    .where(Message.conversation_id == conversation_id)
                )
                # Leave room for one user question and one bounded answer.
                if count + 2 > MAX_MESSAGES:
                    raise _storage_exhausted()
                await self._ensure_message_capacity(db, question_bytes)
                ordinal = await db.scalar(
                    select(func.coalesce(func.max(Message.ordinal) + 1, 0)).where(
                        Message.conversation_id == conversation_id
                    )
                )
                db.add(
                    AgentRun(
                        id=run_id,
                        conversation_id=conversation_id,
                        job_id=job.id,
                        actor_id=actor.id,
                        thread_id=uuid4(),
                        graph_version=_GRAPH_VERSION,
                        selection=chosen_data,
                        status="queued",
                        created_at=now,
                        queued_deadline=now + QUEUE_DEADLINE,
                        updated_at=now,
                    )
                )
                db.add(
                    Message(
                        id=message_id,
                        conversation_id=conversation_id,
                        run_id=run_id,
                        ordinal=ordinal,
                        role="user",
                        body=question,
                        body_bytes=question_bytes,
                        created_at=now,
                    )
                )
                await db.flush()
                await append_event(db, job, "queued")
                await write_audit(
                    db,
                    AuditEntry(
                        actor_id=actor.id,
                        action="job.submitted",
                        resource_type="job",
                        resource_id=job.id,
                        job_id=job.id,
                        outcome="success",
                    ),
                )
                return RunReceipt(run_id=run_id, job_id=job.id, message_id=message_id)
        except AppError as error:
            if error.code == "idempotency_conflict":
                async with self.factory.begin() as db:
                    await write_audit(
                        db,
                        AuditEntry(
                            actor_id=actor.id,
                            action="job.submitted",
                            resource_type="job",
                            outcome="failure",
                            reason_code="idempotency_conflict",
                        ),
                    )
            raise

    async def _replay_submit(self, db, actor, key, input_hash):
        replay = await db.scalar(
            select(Job).where(
                Job.requested_by == actor.id,
                Job.kind == _KIND,
                Job.idempotency_key == key,
            )
        )
        if replay is None or replay.input_hash != input_hash:
            raise _conflict()
        run_id = await db.scalar(
            select(AgentRun.id).where(AgentRun.job_id == replay.id)
        )
        if run_id is None:
            raise not_found()
        message_id = await db.scalar(
            select(Message.id).where(Message.run_id == run_id, Message.role == "user")
        )
        if message_id is None:
            raise not_found()
        return RunReceipt(run_id=run_id, job_id=replay.id, message_id=message_id)

    async def finish(self, session, lease, run_id: UUID, outcome) -> AgentRun:
        """Terminalize a run and its Job in the caller's transaction.

        The assistant body never comes from the model output: it is read back
        from the succeeded ``answer`` ToolCall of the same run. Retiring the
        accepted config in the same statement that publishes the terminal state
        is what makes the official checkpoint rows unreachable, even before the
        cleanup sweep deletes them.
        """

        if not isinstance(run_id, UUID):
            raise _invalid()
        try:
            outcome = RunOutcome.model_validate(outcome)
        except ValidationError:
            raise _invalid() from None
        answered = outcome.status == "answered"
        failed = outcome.status == "failed"
        if answered != (outcome.answer_call_id is not None) or failed != (
            outcome.error_code is not None
        ):
            raise _invalid()
        error_code = outcome.error_code
        if error_code is not None:
            try:
                error_code = _ERROR.validate_python(error_code)
            except ValidationError:
                raise _invalid() from None

        job, now = await Queue(self.factory)._owned(session, lease)
        if job is None:
            raise _lease_expired()
        run = await session.scalar(
            select(AgentRun)
            .where(AgentRun.id == run_id, AgentRun.job_id == lease.job_id)
            .with_for_update()
        )
        if run is None or run.terminal_at is not None:
            raise _lease_expired()

        if answered:
            call = await session.scalar(
                select(ToolCall).where(
                    ToolCall.id == outcome.answer_call_id,
                    ToolCall.run_id == run.id,
                    ToolCall.kind == "answer",
                    ToolCall.status == "succeeded",
                )
            )
            body, manifest = self._answer_body(call)
            manifest_bytes = (
                0
                if manifest is None
                else len(
                    json.dumps(
                        manifest, separators=(",", ":"), ensure_ascii=False
                    ).encode("utf-8")
                )
            )
            await self._ensure_message_capacity(
                session, len(body.encode("utf-8")) + manifest_bytes
            )
            ordinal = await session.scalar(
                select(func.coalesce(func.max(Message.ordinal) + 1, 0)).where(
                    Message.conversation_id == run.conversation_id
                )
            )
            session.add(
                Message(
                    id=uuid4(),
                    conversation_id=run.conversation_id,
                    run_id=run.id,
                    ordinal=ordinal,
                    role="assistant",
                    body=body,
                    body_bytes=len(body.encode("utf-8")),
                    manifest=manifest,
                    created_at=now,
                )
            )

        run.status = (
            "succeeded" if outcome.status in {"answered", "no_evidence"} else "failed"
        )
        run.terminal_reason = (
            error_code
            if failed
            else "cancelled"
            if outcome.status == "cancelled"
            else outcome.status
        )
        run.terminal_at = now
        run.accepted_retired_at = now
        run.accepted_config = None
        run.updated_at = now

        job.status = "succeeded" if run.status == "succeeded" else "failed"
        job.error_code = error_code if failed else None
        job.result = None
        job.lease_owner = None
        job.lease_expires_at = None
        job.updated_at = now
        await Queue._transition(
            session, job, "job.succeeded" if run.status == "succeeded" else "job.failed"
        )
        return run

    @staticmethod
    def _answer_body(call) -> tuple[str, dict | None]:
        if call is None or not isinstance(call.result, dict):
            raise _answer_invalid()
        body = call.result.get("body")
        manifest = call.result.get("manifest")
        if not isinstance(body, str) or not 1 <= len(body.encode("utf-8")) <= 16384:
            raise _answer_invalid()
        if manifest is not None and not isinstance(manifest, dict):
            raise _answer_invalid()
        if (
            manifest is not None
            and len(
                json.dumps(manifest, separators=(",", ":"), ensure_ascii=False).encode(
                    "utf-8"
                )
            )
            > 65536
        ):
            raise _answer_invalid()
        return body, manifest

    async def retire_controlled_run(
        self, session, job_id: UUID, expected: JobFence, outcome: RunOutcome
    ) -> AgentRun | None:
        """Retire a run whose execution lease is already lost.

        Cancellation and expired-lease recovery call this while they own the Job
        transition. It never invents an answer or touches budgets, receipts or
        evidence; an already terminal run is left exactly as it was.
        """

        return await retire_run(session, job_id, expected, outcome)

    async def history(
        self,
        actor: Actor,
        conversation_id: UUID,
        *,
        cursor: str | None = None,
        limit: int = DEFAULT_HISTORY_LIMIT,
    ) -> MessagePage:
        if (
            not isinstance(conversation_id, UUID)
            or not isinstance(limit, int)
            or isinstance(limit, bool)
            or not 1 <= limit <= MAX_HISTORY_LIMIT
        ):
            raise _invalid()
        after = self._cursor(cursor)
        # Ownership precedes any source lookup; source authorization then
        # covers every participating repository, indexed or not.
        await self.authorize(actor, conversation_id, fresh=False)
        async with self.factory() as db:
            statement = (
                select(Message)
                .where(Message.conversation_id == conversation_id)
                .order_by(Message.ordinal.desc())
                .limit(limit + 1)
            )
            if after is not None:
                statement = statement.where(Message.ordinal < after)
            rows = list(await db.scalars(statement))
        page = rows[:limit]
        return MessagePage(
            messages=tuple(
                MessageView(
                    id=row.id,
                    role=row.role,
                    body=row.body,
                    ordinal=row.ordinal,
                    created_at=row.created_at,
                    citations=self._citation_views(row.manifest),
                )
                for row in page
            ),
            next_cursor=str(page[-1].ordinal) if len(rows) > limit else None,
        )

    async def conversations(self, actor: Actor) -> ConversationList:
        """List only this creator's live conversations; never a message body."""
        async with self.factory() as db:
            rows = list(
                await db.scalars(
                    select(Conversation)
                    .where(
                        Conversation.actor_id == actor.id,
                        Conversation.archived_at.is_(None),
                    )
                    .order_by(Conversation.created_at.desc(), Conversation.id.desc())
                    .limit(MAX_CONVERSATIONS)
                )
            )
            if not rows:
                return ConversationList(conversations=())
            identifiers = tuple(row.id for row in rows)
            member_rows = (
                await db.execute(
                    select(
                        ConversationRepository.conversation_id,
                        ConversationRepository.repository_id,
                    )
                    .where(ConversationRepository.conversation_id.in_(identifiers))
                    .order_by(
                        ConversationRepository.conversation_id,
                        ConversationRepository.ordinal,
                    )
                )
            ).all()
            runs = list(
                await db.scalars(
                    select(AgentRun)
                    .where(AgentRun.conversation_id.in_(identifiers))
                    .order_by(AgentRun.created_at.desc(), AgentRun.id.desc())
                )
            )
        members = {identifier: [] for identifier in identifiers}
        for conversation_id, repository_id in member_rows:
            members[conversation_id].append(repository_id)
        latest, active = {}, {}
        for run in runs:
            latest.setdefault(run.conversation_id, run)
            if run.terminal_at is None:
                active.setdefault(run.conversation_id, run)
        return ConversationList(
            conversations=tuple(
                ConversationSummary(
                    id=row.id,
                    selected_repository_ids=tuple(members[row.id]),
                    created_at=row.created_at,
                    active_run=self._run_summary(active.get(row.id)),
                    latest_run=self._run_summary(latest.get(row.id)),
                )
                for row in rows
            )
        )

    @staticmethod
    def _run_summary(run) -> RunSummary | None:
        if run is None:
            return None
        return RunSummary(run_id=run.id, job_id=run.job_id, status=run.status)

    async def citation(
        self,
        actor: Actor,
        conversation_id: UUID,
        message_id: UUID,
        citation_id: str,
        *,
        citations,
        cursor: str | None = None,
    ):
        """Preview one stored manifest entry after full conversation re-auth."""
        if (
            not isinstance(conversation_id, UUID)
            or not isinstance(message_id, UUID)
            or not isinstance(citation_id, str)
        ):
            raise _invalid()
        await self.authorize(actor, conversation_id, fresh=False)
        async with self.factory() as db:
            message = await db.scalar(
                select(Message).where(
                    Message.id == message_id,
                    Message.conversation_id == conversation_id,
                )
            )
        if message is None or message.role != "assistant" or message.manifest is None:
            raise not_found()
        try:
            manifest = EvidenceManifest.model_validate(message.manifest)
        except ValidationError:
            raise not_found() from None
        return await citations.preview(actor, manifest, citation_id, cursor=cursor)

    async def delete(
        self, actor: Actor, conversation_id: UUID, key: str
    ) -> DeletionReceipt:
        """Archive a conversation, erase its bodies and enqueue checkpoint cleanup.

        Deleting content never releases an unknown reservation, a receipt or a
        monthly aggregate: request identity and possible charges outlive the
        bodies. The single deterministic cleanup Job is idempotent per
        conversation.
        """
        try:
            _KEY.validate_python(key)
        except ValidationError:
            raise _invalid() from None
        if not isinstance(conversation_id, UUID):
            raise _invalid()
        async with self.factory() as db:
            conversation = await db.get(Conversation, conversation_id)
            if conversation is None or conversation.actor_id != actor.id:
                raise not_found()
            members = await self._members(db, conversation_id)
        if not members:
            raise not_found()
        cleanup_key = f"delete:{conversation_id}"
        async with transaction(self.factory) as db:
            conversation = await db.scalar(
                select(Conversation)
                .where(
                    Conversation.id == conversation_id,
                    Conversation.actor_id == actor.id,
                )
                .with_for_update()
            )
            if conversation is None:
                raise not_found()
            if await self._members(db, conversation_id) != members:
                raise not_found()
            existing = await db.scalar(
                select(Job)
                .where(
                    Job.requested_by.is_(None),
                    Job.kind == _CLEANUP_KIND,
                    Job.repository_id == members[0],
                    Job.idempotency_key == cleanup_key,
                )
                .with_for_update()
            )
            if conversation.archived_at is not None and existing is not None:
                return DeletionReceipt(
                    conversation_id=conversation_id, job_id=existing.id
                )
            now = await db.scalar(select(func.clock_timestamp()))
            active = await db.scalar(
                select(AgentRun)
                .where(
                    AgentRun.conversation_id == conversation_id,
                    AgentRun.terminal_at.is_(None),
                )
                .with_for_update()
            )
            if active is not None:
                job = await db.scalar(
                    select(Job).where(Job.id == active.job_id).with_for_update()
                )
                if job is None:
                    raise not_found()
                if job.status not in ("succeeded", "failed"):
                    fence = JobFence(
                        status=job.status,
                        owner=job.lease_owner,
                        generation=job.lease_generation,
                        attempt=job.attempt,
                    )
                    await retire_run(db, job.id, fence, RunOutcome(status="cancelled"))
                    job.status = "failed"
                    job.error_code = "cancelled"
                    job.result = None
                    job.lease_owner = None
                    job.lease_expires_at = None
                    job.updated_at = now
                    await Queue._transition(db, job, "job.failed")
            run_ids = select(AgentRun.id).where(
                AgentRun.conversation_id == conversation_id
            )
            await db.execute(sa_delete(ToolCall).where(ToolCall.run_id.in_(run_ids)))
            await db.execute(
                sa_delete(Message).where(Message.conversation_id == conversation_id)
            )
            conversation.archived_at = now
            conversation.updated_at = now
            input_data = {"version": 1, "limit": CLEANUP_LIMIT}
            input_hash = self._digest(
                {"conversation_id": str(conversation_id), "cleanup": input_data}
            )
            job = await db.scalar(
                insert(Job)
                .values(
                    id=uuid4(),
                    kind=_CLEANUP_KIND,
                    repository_id=members[0],
                    requested_by=None,
                    input=input_data,
                    input_hash=input_hash,
                    idempotency_key=cleanup_key,
                    max_attempts=self.max_attempts,
                )
                .on_conflict_do_nothing(
                    index_elements=["repository_id", "kind", "idempotency_key"],
                    index_where=text("requested_by IS NULL"),
                )
                .returning(Job)
            )
            if job is None:
                if existing is None:
                    raise not_found()
                job = existing
                if job.status not in ("succeeded", "failed"):
                    return DeletionReceipt(
                        conversation_id=conversation_id, job_id=job.id
                    )
                job.status = "queued"
                job.attempt = 0
                job.error_code = None
                job.result = None
                job.lease_owner = None
                job.lease_expires_at = None
                job.updated_at = now
            await append_event(db, job, "queued")
            await db.flush()
            return DeletionReceipt(conversation_id=conversation_id, job_id=job.id)

    async def authorize(self, actor: Actor, conversation_id: UUID, *, fresh: bool):
        async with self.factory() as db:
            conversation = await db.get(Conversation, conversation_id)
            if conversation is None or conversation.actor_id != actor.id:
                raise not_found()
            members = await self._members(db, conversation_id)
        return await self.access.verify(actor, members, fresh=fresh)

    async def _ensure_message_capacity(self, db, incoming: int) -> None:
        """Keep the global body+manifest footprint bounded.

        Expired bodies are the only data ever deleted here; unexpired history
        is never sacrificed to make room. The advisory lock serializes all
        inserts in every worker, so the byte sum cannot pass the cap by racing.
        """
        await db.execute(
            select(func.pg_advisory_xact_lock(func.hashtextextended(_MESSAGE_LOCK, 0)))
        )
        for _ in range(8):
            total = int(
                await db.scalar(
                    select(
                        func.coalesce(
                            func.sum(
                                Message.body_bytes
                                + func.coalesce(
                                    func.octet_length(Message.manifest.cast(Text)), 0
                                )
                            ),
                            0,
                        )
                    )
                )
                or 0
            )
            if total + incoming <= MESSAGE_CAPACITY:
                return
            if not await self._purge_expired_messages(db):
                break
        raise _storage_exhausted()

    async def _purge_expired_messages(self, db) -> int:
        identifiers = list(
            await db.scalars(
                select(Message.id)
                .where(
                    Message.created_at
                    <= func.clock_timestamp() - text("interval '90 days'")
                )
                .order_by(Message.created_at, Message.id)
                .limit(CLEANUP_LIMIT)
                .with_for_update(skip_locked=True)
            )
        )
        if not identifiers:
            return 0
        await db.execute(sa_delete(Message).where(Message.id.in_(identifiers)))
        return len(identifiers)

    @staticmethod
    def _citation_views(manifest) -> tuple[CitationView, ...]:
        if not isinstance(manifest, dict):
            return ()
        try:
            parsed = EvidenceManifest.model_validate(manifest)
        except ValidationError:
            return ()
        return tuple(
            CitationView(
                citation_id=entry.citation_id,
                url=entry.url,
                repository_id=entry.identity.repository_id,
                source_repository_id=entry.identity.source_repository_id,
                path=entry.identity.path,
                sha=entry.identity.sha,
                start_line=entry.location.start_line,
                end_line=entry.location.end_line,
            )
            for entry in parsed.entries
        )

    async def _members(self, db, conversation_id: UUID) -> tuple[UUID, ...]:
        return tuple(
            await db.scalars(
                select(ConversationRepository.repository_id)
                .where(ConversationRepository.conversation_id == conversation_id)
                .order_by(ConversationRepository.ordinal)
            )
        )

    @staticmethod
    def _repositories(values) -> tuple[UUID, ...]:
        if not isinstance(values, (tuple, list)):
            raise TypeError("Repositories must be a sequence")
        repositories = tuple(values)
        if not 1 <= len(repositories) <= MAX_REPOSITORIES:
            raise ValueError("Between one and three repositories are required")
        if len(set(repositories)) != len(repositories):
            raise ValueError("Duplicate repository selection")
        if not all(isinstance(item, UUID) for item in repositories):
            raise ValueError("Repository identifiers must be UUIDs")
        return repositories

    @staticmethod
    def _cursor(cursor: str | None) -> int | None:
        if cursor is None:
            return None
        if (
            not isinstance(cursor, str)
            or not cursor
            or len(cursor) > 19
            or not cursor.isascii()
            or not cursor.isdecimal()
        ):
            raise AppError("invalid_cursor", 400, "Invalid message cursor")
        return int(cursor)

    @staticmethod
    def _digest(value) -> str:
        return hashlib.sha256(
            json.dumps(
                value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            ).encode("utf-8")
        ).hexdigest()


async def retire_run(
    session, job_id: UUID, expected: JobFence, outcome: RunOutcome
) -> AgentRun | None:
    """Retire one controlled run inside the caller's terminal transaction.

    The expected fence must still match the locked Job row. The caller owns the
    surrounding transaction, so any rejection here rolls back the Job
    transition with it.
    """

    try:
        outcome = RunOutcome.model_validate(outcome)
    except ValidationError:
        raise _invalid() from None
    if outcome.status == "answered" or outcome.answer_call_id is not None:
        raise _invalid()
    job = await session.scalar(select(Job).where(Job.id == job_id).with_for_update())
    now = await session.scalar(select(func.clock_timestamp()))
    if job is None:
        raise _lease_expired()
    if (
        job.status,
        job.lease_owner,
        job.lease_generation,
        job.attempt,
    ) != (expected.status, expected.owner, expected.generation, expected.attempt):
        raise _lease_expired()
    run = await session.scalar(
        select(AgentRun).where(AgentRun.job_id == job_id).with_for_update()
    )
    if run is None:
        raise _lease_expired()
    if run.terminal_at is not None:
        return run
    run.status = "failed"
    run.terminal_reason = outcome.error_code or "cancelled"
    run.terminal_at = now
    run.accepted_retired_at = now
    run.accepted_config = None
    run.updated_at = now
    return run
