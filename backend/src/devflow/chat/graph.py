"""One bounded, durable ChatAgent graph over official fenced checkpoints.

The graph is deliberately small: retrieve, assemble, ask the model, and -- only
when the model explicitly asks once -- retrieve and ask again. Every node first
enters an independent short fenced transaction that renews the Job lease check,
enforces the cumulative node/deadline budget and persists the counter *before*
any external work happens. Counters and deadlines therefore never live inside a
rollbackable checkpoint.

Durable trust boundaries:

* The official LangGraph saver is wrapped in ``FencedPostgresSaver`` so a
  checkpoint can only be read or written while the exact Job lease, source
  grant and non-terminal run still hold.
* A retrieval result and a validated model answer are stored as bounded
  ``ToolCall`` rows (at most 128 KiB), and the graph state only carries their
  IDs. A restarted attempt re-reads trusted rows instead of trusting the model.
* A model request is reserved and marked dispatched before the single HTTP
  attempt. A settled receipt is replayed; any unknown outcome terminates the
  run and keeps the reservation.
* The run's owned scope is persisted in ``AgentRun.scope``; at most one whole
  refetch after a version change is allowed and it consumes the second and
  final retrieval slot.

Only ``ChatProvider.invoke`` performs a network call, and it is never retried
inside the graph. Nothing here claims real provider quality or billing.
"""

import base64
from datetime import timedelta
from typing import TypedDict
from uuid import UUID

import psycopg
from langgraph.graph import END, START, StateGraph
from pydantic import TypeAdapter, ValidationError
from sqlalchemy import select

from devflow.auth.models import User
from devflow.auth.types import Actor
from devflow.chat.budget import BudgetService
from devflow.chat.checkpoints import FencedPostgresSaver, _raw_dsn
from devflow.chat.context import ContextAssembler, ToolExchange
from devflow.chat.models import AgentRun, Message, RequestReceipt
from devflow.chat.models import ToolCall as RunToolCall
from devflow.chat.provider import ChatProvider, PreparedCall, ProviderAnswerError
from devflow.chat.schemas import RunOutcome
from devflow.config import Settings
from devflow.errors import RETRYABLE_ERROR_CODES, AppError
from devflow.jobs.queue import Lease, Queue
from devflow.jobs.schemas import JobErrorCode
from devflow.knowledge.citations import CitationService
from devflow.knowledge.retrieval import RetrievalContext, RetrievalService
from devflow.knowledge.schemas import (
    ChunkLocation,
    CoverageView,
    Evidence,
    EvidenceManifest,
    EvidenceSet,
    FrozenIndex,
    QueryScope,
    QuerySelection,
    RepositoryEpoch,
    SourceGrant,
    SourceIdentity,
)
from devflow.sync.scope import transaction

_KIND = "chat.run"
RUN_SECONDS = 180
MAX_NODES = 8
MAX_RETRIEVALS = 2

_CALLS = TypeAdapter(JobErrorCode)


class _State(TypedDict, total=False):
    """Primitive graph state; every authority-bearing value lives in rows."""

    question: str
    current_call_id: str
    previous_call_id: str | None
    prepared: dict
    tool_exchange: dict
    call_index: int
    answer_call_id: str | None
    outcome_status: str


def _lease_expired() -> AppError:
    return AppError("lease_expired", 409, "Chat run lease expired")


def _timeout() -> AppError:
    return AppError("attempt_timeout", 503, "Chat run deadline exceeded")


def _state_limit() -> AppError:
    return AppError("state_limit", 409, "Chat run node budget exhausted")


def _internal(message: str) -> AppError:
    return AppError("internal_error", 500, message)


def _permission_revoked() -> AppError:
    return AppError("permission_revoked", 403, "Chat run access was revoked")


def _version_changed() -> AppError:
    return AppError("version_changed", 409, "Chat sources changed during the run")


def _not_configured() -> AppError:
    return AppError("model_not_configured", 503, "Cloud chat is not configured")


def _code(value) -> str:
    try:
        return _CALLS.validate_python(value)
    except (ValidationError, TypeError):
        return "internal_error"


def _actor(user: User) -> Actor:
    return Actor(user.id, user.github_id, user.role, user.auth_version)


def _selection(value) -> QuerySelection:
    try:
        return QuerySelection.model_validate(value)
    except (TypeError, ValueError, ValidationError):
        raise _internal("Run selection is invalid") from None


def _dump_actor(actor: Actor) -> dict:
    return {
        "id": str(actor.id),
        "github_id": actor.github_id,
        "role": actor.role,
        "auth_version": actor.auth_version,
    }


def _load_actor(value) -> Actor:
    return Actor(
        id=UUID(value["id"]),
        github_id=value["github_id"],
        role=value["role"],
        auth_version=value["auth_version"],
    )


def _dump_scope(scope: QueryScope) -> dict:
    return {
        "actor": _dump_actor(scope.actor),
        "grant": {
            "actor": _dump_actor(scope.grant.actor),
            "repository_epochs": [
                {
                    "repository_id": str(epoch.repository_id),
                    "installation_id": epoch.installation_id,
                    "user_version": epoch.user_version,
                    "installation_version": epoch.installation_version,
                    "repository_version": epoch.repository_version,
                }
                for epoch in scope.grant.repository_epochs
            ],
        },
        "selected_repository_ids": [
            str(item) for item in scope.selected_repository_ids
        ],
        "indexes": [
            {
                "repository_id": str(item.repository_id),
                "index_version_id": str(item.index_version_id),
                "sha": item.sha,
                "index_generation": item.index_generation,
                "target_generation": item.target_generation,
                "member_digest": item.member_digest,
            }
            for item in scope.indexes
        ],
        "selection": (
            scope.selection.model_dump(mode="json")
            if scope.selection is not None
            else None
        ),
    }


def _load_scope(value) -> QueryScope:
    try:
        grant = value["grant"]
        selection = value.get("selection")
        return QueryScope(
            actor=_load_actor(value["actor"]),
            grant=SourceGrant(
                actor=_load_actor(grant["actor"]),
                repository_epochs=tuple(
                    RepositoryEpoch(
                        repository_id=UUID(row["repository_id"]),
                        installation_id=row["installation_id"],
                        user_version=row["user_version"],
                        installation_version=row["installation_version"],
                        repository_version=row["repository_version"],
                    )
                    for row in grant["repository_epochs"]
                ),
            ),
            selected_repository_ids=tuple(
                UUID(item) for item in value["selected_repository_ids"]
            ),
            indexes=tuple(
                FrozenIndex(
                    repository_id=UUID(row["repository_id"]),
                    index_version_id=UUID(row["index_version_id"]),
                    sha=row["sha"],
                    index_generation=row["index_generation"],
                    target_generation=row["target_generation"],
                    member_digest=row["member_digest"],
                )
                for row in value["indexes"]
            ),
            selection=(
                QuerySelection.model_validate(selection)
                if selection is not None
                else None
            ),
        )
    except (KeyError, TypeError, ValueError, AttributeError, ValidationError):
        raise _internal("Stored query scope is invalid") from None


def _dump_evidence(evidence: EvidenceSet) -> dict:
    return {
        "status": evidence.status,
        "scope": _dump_scope(evidence.scope),
        "coverage": [item.model_dump(mode="json") for item in evidence.coverage],
        "evidence": [
            {
                "identity": item.identity.model_dump(mode="json"),
                "chunk_id": str(item.chunk_id),
                "index_version_id": str(item.index_version_id),
                "location": item.location.model_dump(mode="json"),
                "text": item.text,
                "content_hash": item.content_hash,
                "rank": item.rank,
            }
            for item in evidence.evidence
        ],
    }


def _evidence_from(value) -> EvidenceSet:
    try:
        return EvidenceSet(
            scope=_load_scope(value["scope"]),
            status=value["status"],
            evidence=tuple(
                Evidence(
                    identity=SourceIdentity.model_validate(row["identity"]),
                    chunk_id=UUID(row["chunk_id"]),
                    index_version_id=UUID(row["index_version_id"]),
                    location=ChunkLocation.model_validate(row["location"]),
                    text=row["text"],
                    content_hash=row["content_hash"],
                    rank=row["rank"],
                )
                for row in value["evidence"]
            ),
            coverage=tuple(
                CoverageView.model_validate(row) for row in value["coverage"]
            ),
        )
    except (KeyError, TypeError, ValueError, AttributeError, ValidationError):
        raise _internal("Stored evidence is invalid") from None


def _dump_prepared(prepared: PreparedCall) -> dict:
    return {
        "body": base64.b64encode(prepared.body).decode("ascii"),
        "request_hash": prepared.request_hash,
        "parameter_version": prepared.parameter_version,
        "manifest": prepared.manifest.model_dump(mode="json"),
    }


def _load_prepared(state: _State) -> PreparedCall:
    payload = state.get("prepared")
    try:
        return PreparedCall(
            messages=(),
            tools=(),
            manifest=EvidenceManifest.model_validate(payload["manifest"]),
            body=base64.b64decode(payload["body"], validate=True),
            request_hash=payload["request_hash"],
            parameter_version=payload["parameter_version"],
        )
    except (KeyError, TypeError, ValueError, ValidationError):
        raise _internal("Prepared request is invalid") from None


def _exchange_from(value) -> dict:
    try:
        return {
            "tool_call_id": value["tool_call_id"],
            "query": value["query"],
        }
    except (KeyError, TypeError):
        raise _internal("Stored tool request is invalid") from None


def _outcome(state: _State) -> RunOutcome:
    status = state.get("outcome_status")
    if status == "no_evidence":
        return RunOutcome(status="no_evidence")
    if status == "answered":
        try:
            return RunOutcome(
                status="answered", answer_call_id=UUID(state["answer_call_id"])
            )
        except (KeyError, TypeError, ValueError):
            return RunOutcome(status="failed", error_code="internal_error")
    return RunOutcome(status="failed", error_code=_code(status))


class ChatGraph:
    """Execute one claimed chat.run Job to a terminal ``RunOutcome``."""

    def __init__(
        self,
        factory,
        settings: Settings,
        *,
        retrieval: RetrievalService,
        citations: CitationService,
        budget: BudgetService,
        provider: ChatProvider | None,
    ) -> None:
        self.factory = factory
        self.settings = settings
        self.retrieval = retrieval
        self.citations = citations
        self.budget = budget
        self.provider = provider

    async def execute(self, lease: Lease, run_id: UUID) -> RunOutcome:
        if not isinstance(run_id, UUID):
            return RunOutcome(status="failed", error_code="internal_error")
        try:
            thread_id, resume, question, actor, selection = await self._begin(
                lease, run_id
            )
            grant = await self.retrieval.access.verify(
                actor, selection.selected_repository_ids, fresh=True
            )
            connection = await psycopg.AsyncConnection.connect(
                _raw_dsn(self.settings), autocommit=True
            )
            try:
                saver = FencedPostgresSaver(
                    connection, lease, run_id, grant, settings=self.settings
                )
                graph = self._compile(lease, run_id, saver)
                payload = None if resume else {"question": question}
                state = await graph.ainvoke(
                    payload,
                    {"configurable": {"thread_id": str(thread_id)}},
                    durability="sync",
                )
            finally:
                await connection.close()
        except AppError as error:
            if error.code in RETRYABLE_ERROR_CODES:
                raise
            return RunOutcome(status="failed", error_code=_code(error.code))
        return _outcome(state)

    # ------------------------------------------------------------ graph wiring

    def _compile(self, lease: Lease, run_id: UUID, saver) -> object:
        builder = StateGraph(_State)

        async def retrieve_initial(state: _State) -> dict:
            return await self._retrieve_initial(lease, run_id, state)

        async def assemble_initial(state: _State) -> dict:
            return await self._assemble_initial(lease, run_id, state)

        async def model_first(state: _State) -> dict:
            return await self._model(lease, run_id, state, final=False)

        async def retrieve_more(state: _State) -> dict:
            return await self._retrieve_more(lease, run_id, state)

        async def assemble_final(state: _State) -> dict:
            return await self._assemble_final(lease, run_id, state)

        async def model_final(state: _State) -> dict:
            return await self._model(lease, run_id, state, final=True)

        async def validate(state: _State) -> dict:
            return await self._validate(lease, run_id, state)

        def after_retrieval(state: _State) -> str:
            return "done" if state.get("outcome_status") else "assemble_initial"

        def after_model(state: _State) -> str:
            if state.get("answer_call_id"):
                return "validate"
            if state.get("outcome_status"):
                return "done"
            return "retrieve_more"

        builder.add_node("retrieve_initial", retrieve_initial)
        builder.add_node("assemble_initial", assemble_initial)
        builder.add_node("model_first", model_first)
        builder.add_node("retrieve_more", retrieve_more)
        builder.add_node("assemble_final", assemble_final)
        builder.add_node("model_final", model_final)
        builder.add_node("validate", validate)

        builder.add_edge(START, "retrieve_initial")
        builder.add_conditional_edges(
            "retrieve_initial",
            after_retrieval,
            {"done": END, "assemble_initial": "assemble_initial"},
        )
        builder.add_edge("assemble_initial", "model_first")
        builder.add_conditional_edges(
            "model_first",
            after_model,
            {
                "validate": "validate",
                "done": END,
                "retrieve_more": "retrieve_more",
            },
        )
        builder.add_edge("retrieve_more", "assemble_final")
        builder.add_edge("assemble_final", "model_final")
        builder.add_edge("model_final", "validate")
        builder.add_edge("validate", END)
        return builder.compile(checkpointer=saver)

    # ------------------------------------------------------------- entry nodes

    async def _begin(self, lease: Lease, run_id: UUID):
        async with transaction(self.factory) as db:
            run, now = await self._fenced_run(db, lease, run_id)
            if now >= run.queued_deadline:
                raise _timeout()
            if run.started_at is None:
                # The first execution owns one fixed window; a restart never
                # extends it.
                run.started_at = now
                run.deadline = now + timedelta(seconds=RUN_SECONDS)
            if run.deadline is None or now >= run.deadline:
                raise _timeout()
            run.status = "running"
            run.updated_at = now
            user = await db.scalar(select(User).where(User.id == run.actor_id))
            if user is None or not user.enabled:
                raise _permission_revoked()
            message = await db.scalar(
                select(Message).where(Message.run_id == run_id, Message.role == "user")
            )
            if message is None:
                raise _internal("Chat run question is missing")
            return (
                run.thread_id,
                run.accepted_config is not None,
                message.body,
                _actor(user),
                _selection(run.selection),
            )

    async def _enter(self, lease: Lease, run_id: UUID):
        """Charge one node entry in its own committed transaction."""
        async with transaction(self.factory) as db:
            run, now = await self._fenced_run(db, lease, run_id)
            if run.deadline is None or now >= run.deadline:
                raise _timeout()
            if run.nodes_used >= MAX_NODES:
                raise _state_limit()
            run.nodes_used += 1
            run.status = "running"
            run.updated_at = now
            user = await db.scalar(select(User).where(User.id == run.actor_id))
            if user is None or not user.enabled:
                raise _permission_revoked()
            return run.deadline, _actor(user), _selection(run.selection)

    async def _fenced_run(self, db, lease: Lease, run_id: UUID):
        job, now = await Queue(self.factory)._owned(db, lease)
        if job is None:
            raise _lease_expired()
        if job.kind != _KIND:
            raise _internal("Not a chat run Job")
        run = await db.scalar(
            select(AgentRun)
            .where(AgentRun.id == run_id, AgentRun.job_id == job.id)
            .with_for_update()
        )
        if (
            run is None
            or run.terminal_at is not None
            or run.actor_id != job.requested_by
        ):
            raise _lease_expired()
        return run, now

    def _provider(self) -> ChatProvider:
        if self.provider is None:
            raise _not_configured()
        return self.provider

    # ------------------------------------------------------------- retrieval

    def _reserver(self, lease: Lease, run_id: UUID):
        """Atomically consume one retrieval slot before any search starts.

        A restarted whole refetch may happen at most once and must fit in the
        second slot. The first slot is consumed before the injection, so a
        version change during the first retrieval consumes the second slot
        exactly once.
        """

        async def reserve(restarted: bool) -> None:
            async with transaction(self.factory) as db:
                run, now = await self._fenced_run(db, lease, run_id)
                if run.deadline is None or now >= run.deadline:
                    raise _timeout()
                envelope = dict(run.scope) if isinstance(run.scope, dict) else {}
                if restarted and envelope.get("refreshed") is True:
                    raise _version_changed()
                if run.retrievals_used >= MAX_RETRIEVALS:
                    raise _version_changed()
                run.retrievals_used += 1
                if restarted:
                    envelope["refreshed"] = True
                    run.scope = envelope
                run.updated_at = now

        return reserve

    async def _retrieve_initial(
        self, lease: Lease, run_id: UUID, state: _State
    ) -> dict:
        deadline, actor, selection = await self._enter(lease, run_id)
        question = state.get("question")
        if not isinstance(question, str):
            raise _internal("Chat question is missing")
        scope = await self.retrieval.freeze(actor, selection, fresh=True)
        context = RetrievalContext(
            lease=lease,
            scope=scope,
            deadline=deadline,
            reserve_attempt=self._reserver(lease, run_id),
        )
        try:
            evidence = await self.retrieval.retrieve(context, question)
        except AppError as error:
            await self._failed_retrieval(lease, run_id, 0, error)
            raise
        call_id = await self._write_call(
            lease,
            run_id,
            0,
            "retrieval",
            {"call_index": 0},
            _dump_evidence(evidence),
            scope=evidence.scope,
        )
        update: dict = {"current_call_id": str(call_id), "call_index": 0}
        if evidence.status == "no_evidence":
            update["outcome_status"] = "no_evidence"
        return update

    async def _retrieve_more(self, lease: Lease, run_id: UUID, state: _State) -> dict:
        deadline, _, _ = await self._enter(lease, run_id)
        previous = await self._load_evidence(run_id, state.get("current_call_id"))
        exchange = state.get("tool_exchange")
        if not isinstance(exchange, dict):
            raise _internal("Tool exchange is missing")
        query = exchange.get("query")
        if not isinstance(query, str):
            raise _internal("Tool query is missing")
        context = RetrievalContext(
            lease=lease,
            scope=previous.scope,
            deadline=deadline,
            reserve_attempt=self._reserver(lease, run_id),
        )
        try:
            evidence = await self.retrieval.retrieve(context, query)
        except AppError as error:
            await self._failed_retrieval(lease, run_id, 2, error)
            raise
        call_id = await self._write_call(
            lease,
            run_id,
            2,
            "retrieval",
            {"call_index": 1},
            _dump_evidence(evidence),
            scope=evidence.scope,
        )
        return {
            "previous_call_id": state["current_call_id"],
            "current_call_id": str(call_id),
            "call_index": 1,
        }

    async def _failed_retrieval(
        self,
        lease: Lease,
        run_id: UUID,
        ordinal: int,
        error: AppError,
    ) -> None:
        # A retryable retrieval failure leaves no row so the resumed node can
        # search again; a terminal one is recorded for honest evidence.
        if error.code in RETRYABLE_ERROR_CODES:
            return
        await self._write_call(
            lease,
            run_id,
            ordinal,
            "retrieval",
            {"call_index": ordinal // 2},
            {"error_code": _code(error.code)},
            status="failed",
        )

    # ------------------------------------------------------------- assembly

    async def _assemble_initial(
        self, lease: Lease, run_id: UUID, state: _State
    ) -> dict:
        await self._enter(lease, run_id)
        provider = self._provider()
        current = await self._load_evidence(run_id, state.get("current_call_id"))
        prepared = await ContextAssembler(self.citations, provider).assemble(
            state["question"], current
        )
        return {"prepared": _dump_prepared(prepared)}

    async def _assemble_final(self, lease: Lease, run_id: UUID, state: _State) -> dict:
        await self._enter(lease, run_id)
        provider = self._provider()
        current = await self._load_evidence(run_id, state.get("current_call_id"))
        previous = await self._load_evidence(run_id, state.get("previous_call_id"))
        exchange = state.get("tool_exchange")
        try:
            record = ToolExchange(
                tool_call_id=exchange["tool_call_id"],
                query=exchange["query"],
                status=current.status,
                evidence_count=len(current.evidence),
            )
        except (KeyError, TypeError, ValueError):
            raise _internal("Tool exchange is invalid") from None
        prepared = await ContextAssembler(self.citations, provider).assemble(
            state["question"],
            current,
            previous=previous,
            tool_exchange=record,
        )
        return {"prepared": _dump_prepared(prepared)}

    # ------------------------------------------------------------------ model

    async def _model(
        self, lease: Lease, run_id: UUID, state: _State, *, final: bool
    ) -> dict:
        deadline, _, _ = await self._enter(lease, run_id)
        call_index = 1 if final else 0
        existing = await self._load_model_call(run_id, call_index)
        if existing is not None:
            return await self._replay_model(lease, run_id, state, existing, final=final)
        provider = self._provider()
        current = await self._load_evidence(run_id, state.get("current_call_id"))
        await self.retrieval.revalidate(current.scope)
        prepared = _load_prepared(state)
        reservation = await self.budget.reserve(
            lease,
            run_id,
            f"model:{call_index}",
            prepared.request_hash,
            prepared.parameter_version,
        )
        await self.budget.mark_dispatched(lease, reservation.receipt_id)
        try:
            reply = await provider.invoke(prepared, deadline=deadline)
        except ProviderAnswerError as error:
            if error.usage is None:
                await self.budget.mark_unknown(lease, reservation.receipt_id)
            else:
                await self.budget.settle(lease, reservation.receipt_id, error.usage)
            raise
        except AppError as error:
            if error.code == "model_call_outcome_unknown":
                await self.budget.mark_unknown(lease, reservation.receipt_id)
            raise
        if reply.usage is None:
            await self.budget.mark_unknown(lease, reservation.receipt_id)
            return {"outcome_status": "model_call_outcome_unknown"}

        if reply.answer is not None:
            try:
                self.citations.validate_ids(
                    prepared.manifest, reply.answer.citation_ids
                )
            except AppError:
                await self.budget.settle(lease, reservation.receipt_id, reply.usage)
                raise
            result = {
                "body": reply.answer.conclusion,
                "manifest": prepared.manifest.model_dump(mode="json"),
                "usage": reply.usage.model_dump(mode="json"),
                "call_index": call_index,
            }
            call_id = await self._write_call(
                lease,
                run_id,
                2 * call_index + 1,
                "answer",
                {"call_index": call_index},
                result,
            )
            await self.budget.settle(lease, reservation.receipt_id, reply.usage)
            return {"answer_call_id": str(call_id)}

        if reply.tool_query is None or reply.tool_call_id is None:
            await self.budget.settle(lease, reservation.receipt_id, reply.usage)
            raise ProviderAnswerError(reply.usage)
        await self._write_call(
            lease,
            run_id,
            2 * call_index + 1,
            "tool_request",
            {
                "call_index": call_index,
                "tool_call_id": reply.tool_call_id,
                "query": reply.tool_query,
            },
            {"status": "requested"},
        )
        await self.budget.settle(lease, reservation.receipt_id, reply.usage)
        if final:
            return {"outcome_status": "answer_validation_failed"}
        return {
            "tool_exchange": {
                "tool_call_id": reply.tool_call_id,
                "query": reply.tool_query,
            },
            "call_index": 1,
        }

    async def _replay_model(
        self,
        lease: Lease,
        run_id: UUID,
        state: _State,
        call: RunToolCall,
        *,
        final: bool,
    ) -> dict:
        if call.status == "failed":
            result = call.result if isinstance(call.result, dict) else {}
            return {"outcome_status": _code(result.get("error_code"))}
        if call.status != "succeeded":
            return {"outcome_status": "model_call_outcome_unknown"}
        receipt = await self._load_receipt(run_id, call.ordinal // 2)
        if receipt is None:
            raise _internal("Chat receipt is missing")
        if receipt.status == "settled":
            if call.kind == "answer":
                current = await self._load_evidence(
                    run_id, state.get("current_call_id")
                )
                await self.retrieval.revalidate(current.scope)
                return {"answer_call_id": str(call.id)}
            if final:
                return {"outcome_status": "answer_validation_failed"}
            return {
                "tool_exchange": _exchange_from(call.arguments),
                "call_index": 1,
            }
        if receipt.status in ("reserved", "dispatched"):
            await self.budget.mark_unknown(lease, receipt.id)
        return {"outcome_status": "model_call_outcome_unknown"}

    async def _validate(self, lease: Lease, run_id: UUID, state: _State) -> dict:
        await self._enter(lease, run_id)
        if state.get("answer_call_id"):
            return {"outcome_status": "answered"}
        status = state.get("outcome_status")
        if status:
            return {"outcome_status": status}
        return {"outcome_status": "answer_validation_failed"}

    # ------------------------------------------------------------- persistence

    async def _write_call(
        self,
        lease: Lease,
        run_id: UUID,
        ordinal: int,
        kind: str,
        arguments: dict,
        result: dict,
        *,
        status: str = "succeeded",
        scope: QueryScope | None = None,
    ):
        async with transaction(self.factory) as db:
            run, now = await self._fenced_run(db, lease, run_id)
            existing = await db.scalar(
                select(RunToolCall)
                .where(
                    RunToolCall.run_id == run_id,
                    RunToolCall.ordinal == ordinal,
                )
                .with_for_update()
            )
            if existing is not None:
                return existing.id
            if run.deadline is None:
                raise _internal("Chat run has no deadline")
            if scope is not None:
                envelope = _dump_scope(scope)
                prior = run.scope if isinstance(run.scope, dict) else {}
                envelope["refreshed"] = prior.get("refreshed") is True
                run.scope = envelope
            run.updated_at = now
            call = RunToolCall(
                run_id=run_id,
                ordinal=ordinal,
                kind=kind,
                arguments=arguments,
                result=result,
                status=status,
                created_at=now,
                completed_at=(now if status in ("succeeded", "failed") else None),
                expires_at=run.deadline,
            )
            db.add(call)
            await db.flush()
            return call.id

    async def _load_model_call(self, run_id: UUID, call_index: int):
        async with self.factory() as db:
            return await db.scalar(
                select(RunToolCall).where(
                    RunToolCall.run_id == run_id,
                    RunToolCall.ordinal == 2 * call_index + 1,
                    RunToolCall.kind.in_(("answer", "tool_request")),
                )
            )

    async def _load_evidence(self, run_id: UUID, call_id) -> EvidenceSet:
        try:
            value = UUID(call_id)
        except (TypeError, ValueError, AttributeError):
            raise _internal("Stored tool call id is invalid") from None
        async with self.factory() as db:
            call = await db.scalar(
                select(RunToolCall).where(
                    RunToolCall.id == value,
                    RunToolCall.run_id == run_id,
                    RunToolCall.kind == "retrieval",
                    RunToolCall.status == "succeeded",
                )
            )
        if call is None or not isinstance(call.result, dict):
            raise _internal("Stored retrieval is missing")
        return _evidence_from(call.result)

    async def _load_receipt(self, run_id: UUID, ordinal: int):
        async with self.factory() as db:
            return await db.scalar(
                select(RequestReceipt).where(
                    RequestReceipt.run_id == run_id,
                    RequestReceipt.kind == "model",
                    RequestReceipt.ordinal == ordinal,
                )
            )
