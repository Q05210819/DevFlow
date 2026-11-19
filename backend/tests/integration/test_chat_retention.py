"""Terminal retirement precedes official thread deletion.

Every helper talks to the isolated real PostgreSQL. The sweep only deletes the
official LangGraph rows after the accepted pointer has been retired, keeps
active runs and unknown-budget evidence intact, and rejects writes from a stale
lease even while cleanup is running.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from support.chat_env import chat_env as chat_env  # noqa: PLC0414
from support.checkpoint_env import (
    active_run,
    checkpoint,
    claim_cleanup,
    config,
    connect,
    enqueue_cleanup,
    finish_run,
    official_counts,
    versions,
)
from support.checkpoint_env import (
    checkpoint_env as checkpoint_env,  # noqa: PLC0414
)
from support.job_env import job_env as job_env  # noqa: PLC0414
from support.knowledge_env import knowledge_env as knowledge_env  # noqa: PLC0414
from support.knowledge_env import repo_env as repo_env  # noqa: PLC0414

from devflow.chat import checkpoints
from devflow.chat.budget import BudgetService
from devflow.chat.checkpoints import FencedPostgresSaver
from devflow.chat.models import (
    AgentRun,
    BudgetLedger,
    CheckpointAccount,
    Conversation,
    Message,
    RequestReceipt,
    ToolCall,
)
from devflow.chat.retention import ChatRetention
from devflow.chat.schemas import RunOutcome
from devflow.errors import AppError
from devflow.jobs.models import Job
from devflow.jobs.schemas import ChatCleanupResult

pytestmark = pytest.mark.asyncio

PRICING = {
    "chat_cloud_enabled": True,
    "chat_budget_currency": "USD",
    "chat_run_budget": Decimal("1.000000"),
    "chat_month_budget": Decimal("2.000000"),
    "chat_budget_minor_unit": Decimal("0.000001"),
    "chat_pricing_version": "test-pricing-v1",
    "chat_pricing_valid_until": datetime.now(UTC) + timedelta(days=1),
    "chat_input_price_per_million": Decimal("0.500000"),
    "chat_output_price_per_million": Decimal("1.500000"),
}


def priced(env):
    return BudgetService(env.factory, env.settings.model_copy(update=PRICING))


async def answer_call(env, run_id, body="answer", manifest=None, *, ordinal=0):
    async with env.factory.begin() as db:
        call = ToolCall(
            run_id=run_id,
            ordinal=ordinal,
            kind="answer",
            arguments={"version": 1},
            result={"body": body, "manifest": manifest},
            status="succeeded",
            completed_at=datetime.now(UTC),
            expires_at=datetime.now(UTC) + timedelta(days=1),
        )
        db.add(call)
        await db.flush()
        return call.id


async def run_row(env, run_id):
    async with env.factory() as db:
        return await db.scalar(select(AgentRun).where(AgentRun.id == run_id))


async def account_row(env, run_id):
    async with env.factory() as db:
        return await db.get(CheckpointAccount, run_id)


async def messages_of(env, run_id):
    async with env.factory() as db:
        return list(await db.scalars(select(Message).where(Message.run_id == run_id)))


async def cleanup_job(env):
    async with env.factory() as db:
        return await db.scalar(select(Job).where(Job.kind == "chat.cleanup"))


async def ledger_totals(env, run_id):
    async with env.factory() as db:
        run = await db.scalar(select(BudgetLedger).where(BudgetLedger.run_id == run_id))
        month = await db.scalar(
            select(BudgetLedger).where(
                BudgetLedger.ledger_scope == "month",
                BudgetLedger.actor_id == env.actor.id,
            )
        )
    assert run is not None and month is not None
    return run.reserved, run.settled, month.reserved, month.settled


async def sweep_once(env):
    await enqueue_cleanup(env)
    lease = await claim_cleanup(env)
    return await ChatRetention(env.factory).sweep(lease)


async def test_terminal_retire_precedes_thread_delete(checkpoint_env):
    env = checkpoint_env
    run = await active_run(env, "retire")
    checkpoint_id = uuid4()
    stored = checkpoint({"messages": []}, checkpoint_id=checkpoint_id)

    async with connect(env.settings) as connection:
        saver = FencedPostgresSaver(connection, run.lease, run.run_id, run.grant)
        accepted = await saver.aput(
            config(run.thread_id), stored, {"step": 1}, versions(stored)
        )
        await saver.aput_writes(accepted, [("messages", {"text": "pending"})], "task-1")
        assert await official_counts(connection, run.thread_id) == (1, 1, 1)
        account = await account_row(env, run.run_id)
        assert account is not None and account.checkpoint_count == 1
        assert account.bytes > 0

        answer_id = await answer_call(env, run.run_id, "The answer.", {"entries": []})
        await finish_run(
            env,
            run.lease,
            run.run_id,
            RunOutcome(status="answered", answer_call_id=answer_id),
        )
        finished = await run_row(env, run.run_id)
        assert finished is not None
        assert finished.status == "succeeded"
        assert finished.terminal_reason == "answered"
        assert finished.terminal_at is not None
        assert finished.accepted_retired_at is not None
        assert finished.accepted_config is None

        # Retirement ends writability but never deletes the thread itself.
        assert await official_counts(connection, run.thread_id) == (1, 1, 1)
        with pytest.raises(AppError) as error:
            await saver.aput_writes(
                accepted, [("messages", {"text": "late"})], "task-late"
            )
        assert error.value.code == "lease_expired"

    job = await cleanup_job(env)
    assert job is None
    assistant = [
        message
        for message in await messages_of(env, run.run_id)
        if message.role == "assistant"
    ]
    assert len(assistant) == 1
    assert assistant[0].body == "The answer."
    assert assistant[0].body_bytes == len(b"The answer.")
    assert assistant[0].manifest == {"entries": []}

    result = await sweep_once(env)
    assert result.examined == 1
    assert result.reclaimed == 1
    assert result.bytes_reclaimed > 0

    async with connect(env.settings) as connection:
        assert await official_counts(connection, run.thread_id) == (0, 0, 0)
    assert await account_row(env, run.run_id) is None
    retained = await run_row(env, run.run_id)
    assert retained is not None
    assert {message.role for message in await messages_of(env, run.run_id)} == {
        "user",
        "assistant",
    }
    job = await cleanup_job(env)
    assert job is not None and job.status == "succeeded"
    stored_result = ChatCleanupResult.model_validate(job.result)
    assert stored_result.examined == 1
    assert stored_result.reclaimed == 1
    assert stored_result.bytes_reclaimed == result.bytes_reclaimed


async def test_cleanup_protects_active_and_unknown_budget(checkpoint_env):
    env = checkpoint_env

    active = await active_run(env, "active")
    async with connect(env.settings) as connection:
        active_saver = FencedPostgresSaver(
            connection, active.lease, active.run_id, active.grant
        )
        stored = checkpoint({"messages": []})
        await active_saver.aput(
            config(active.thread_id), stored, {"step": 1}, versions(stored)
        )
        active_counts = await official_counts(connection, active.thread_id)

        unknown = await active_run(env, "unknown")
        priced_service = priced(env)
        reservation = await priced_service.reserve(
            unknown.lease, unknown.run_id, "model:0", "b" * 64, "params-v1"
        )
        await priced_service.mark_dispatched(unknown.lease, reservation.receipt_id)
        await priced_service.mark_unknown(unknown.lease, reservation.receipt_id)
        before = await ledger_totals(env, unknown.run_id)
        unknown_saver = FencedPostgresSaver(
            connection, unknown.lease, unknown.run_id, unknown.grant
        )
        unknown_stored = checkpoint({"messages": ["partial"]})
        await unknown_saver.aput(
            config(unknown.thread_id),
            unknown_stored,
            {"step": 1},
            versions(unknown_stored),
        )
        await finish_run(
            env,
            unknown.lease,
            unknown.run_id,
            RunOutcome(
                status="failed",
                answer_call_id=None,
                error_code="model_call_outcome_unknown",
            ),
        )

        result = await sweep_once(env)
        assert result.reclaimed == 1

        # The active thread was never a candidate.
        assert await official_counts(connection, active.thread_id) == active_counts
        assert await account_row(env, active.run_id) is not None
        assert (await run_row(env, active.run_id)).terminal_at is None

        # The unknown receipt outlives the thread it was reserved for.
        assert await official_counts(connection, unknown.thread_id) == (0, 0, 0)
        assert await account_row(env, unknown.run_id) is None
        async with env.factory() as db:
            retained = await db.get(RequestReceipt, reservation.receipt_id)
        assert retained is not None and retained.status == "unknown"
        assert await ledger_totals(env, unknown.run_id) == before
        failed = await run_row(env, unknown.run_id)
        assert failed is not None
        assert failed.status == "failed"
        assert failed.terminal_reason == "model_call_outcome_unknown"
        async with env.factory() as db:
            job = await db.get(Job, unknown.lease.job_id)
        assert job is not None and job.status == "failed"


async def test_checkpoint_accounting_boundaries(checkpoint_env):
    env = checkpoint_env
    assert checkpoints.MAX_CHECKPOINT_BYTES == 64 * 1024
    assert checkpoints.MAX_CHECKPOINTS_PER_RUN == 32
    assert checkpoints.MAX_GLOBAL_BYTES == 128 * 1024 * 1024

    run = await active_run(env, "boundaries")
    oversized = checkpoint({"messages": "x" * (64 * 1024)})
    async with connect(env.settings) as connection:
        saver = FencedPostgresSaver(connection, run.lease, run.run_id, run.grant)
        with pytest.raises(AppError) as error:
            await saver.aput(
                config(run.thread_id), oversized, {"step": 1}, versions(oversized)
            )
        assert error.value.code == "state_limit"
        assert await official_counts(connection, run.thread_id) == (0, 0, 0)

        accepted = None
        for index in range(32):
            value = checkpoint({f"channel_{index}": [index]})
            accepted = await saver.aput(
                config(run.thread_id), value, {"step": index + 1}, versions(value)
            )
        assert await official_counts(connection, run.thread_id) == (32, 32, 0)
        extra = checkpoint({"channel_extra": [99]})
        with pytest.raises(AppError) as error:
            await saver.aput(
                config(run.thread_id), extra, {"step": 33}, versions(extra)
            )
        assert error.value.code == "state_limit"
        assert await official_counts(connection, run.thread_id) == (32, 32, 0)
    current = await run_row(env, run.run_id)
    assert current is not None and current.accepted_config == accepted


async def test_global_cap_evicts_only_earlier_terminal_threads(
    checkpoint_env, monkeypatch
):
    env = checkpoint_env

    terminal = await active_run(env, "evict-terminal")
    terminal_ids = []
    async with connect(env.settings) as connection:
        terminal_saver = FencedPostgresSaver(
            connection, terminal.lease, terminal.run_id, terminal.grant
        )
        for index in range(3):
            value = checkpoint({f"terminal_{index}": [index]})
            await terminal_saver.aput(
                config(terminal.thread_id), value, {"step": index + 1}, versions(value)
            )
            terminal_ids.append(UUID(value["id"]))
        answer_id = await answer_call(env, terminal.run_id, "done")
        await finish_run(
            env,
            terminal.lease,
            terminal.run_id,
            RunOutcome(status="answered", answer_call_id=answer_id),
        )

        writer = await active_run(env, "evict-writer")
        writer_saver = FencedPostgresSaver(
            connection, writer.lease, writer.run_id, writer.grant
        )
        first = checkpoint({"messages": [1]})
        await writer_saver.aput(
            config(writer.thread_id), first, {"step": 1}, versions(first)
        )
        terminal_account = await account_row(env, terminal.run_id)
        writer_account = await account_row(env, writer.run_id)
        assert terminal_account is not None and writer_account is not None
        terminal_bytes = terminal_account.bytes
        writer_bytes = writer_account.bytes
        monkeypatch.setattr(
            checkpoints, "MAX_GLOBAL_BYTES", terminal_bytes + writer_bytes
        )

        second = checkpoint({"messages": [2]})
        await writer_saver.aput(
            config(writer.thread_id), second, {"step": 2}, versions(second)
        )
        assert await official_counts(connection, terminal.thread_id) == (0, 0, 0)
        assert await official_counts(connection, writer.thread_id) == (2, 2, 0)

        monkeypatch.setattr(checkpoints, "MAX_GLOBAL_BYTES", 1)
        third = checkpoint({"messages": [3]})
        with pytest.raises(AppError) as error:
            await writer_saver.aput(
                config(writer.thread_id), third, {"step": 3}, versions(third)
            )
        assert error.value.code == "state_limit"
        assert await official_counts(connection, writer.thread_id) == (2, 2, 0)

    assert await account_row(env, terminal.run_id) is None
    assert await account_row(env, writer.run_id) is not None
    current = await run_row(env, writer.run_id)
    assert current is not None and current.terminal_at is None
    assert current.accepted_config is not None
    assert current.accepted_config["configurable"]["checkpoint_id"] == second["id"]


async def test_stale_writer_loses_the_cleanup_race(checkpoint_env):
    env = checkpoint_env

    terminal = await active_run(env, "race-terminal")
    async with connect(env.settings) as terminal_connection:
        terminal_saver = FencedPostgresSaver(
            terminal_connection, terminal.lease, terminal.run_id, terminal.grant
        )
        stored = checkpoint({"messages": []})
        accepted = await terminal_saver.aput(
            config(terminal.thread_id), stored, {"step": 1}, versions(stored)
        )
        answer_id = await answer_call(env, terminal.run_id, "finished")
        await finish_run(
            env,
            terminal.lease,
            terminal.run_id,
            RunOutcome(status="answered", answer_call_id=answer_id),
        )

        current = await active_run(env, "race-current")
        async with connect(env.settings) as current_connection:
            current_saver = FencedPostgresSaver(
                current_connection, current.lease, current.run_id, current.grant
            )
            first = checkpoint({"messages": [1]})
            await current_saver.aput(
                config(current.thread_id), first, {"step": 1}, versions(first)
            )
            await enqueue_cleanup(env)
            lease = await claim_cleanup(env)
            sweep = ChatRetention(env.factory).sweep(lease)
            stale = terminal_saver.aput_writes(
                accepted, [("messages", {"text": "stale"})], "task-stale"
            )
            second = checkpoint({"messages": [2]})
            live = current_saver.aput(
                config(current.thread_id), second, {"step": 2}, versions(second)
            )
            results = await asyncio.gather(sweep, stale, live, return_exceptions=True)

            assert not isinstance(results[0], BaseException)
            assert results[0].reclaimed == 1
            assert isinstance(results[1], AppError)
            assert results[1].code == "lease_expired"
            assert not isinstance(results[2], BaseException)

            assert await official_counts(terminal_connection, terminal.thread_id) == (
                0,
                0,
                0,
            )
            assert await official_counts(current_connection, current.thread_id) == (
                2,
                2,
                0,
            )
            loaded = await current_saver.aget_tuple(config(current.thread_id))
            assert loaded is not None
            assert loaded.config["configurable"]["checkpoint_id"] == second["id"]

    assert await account_row(env, terminal.run_id) is None
    assert await account_row(env, current.run_id) is not None
    job = await cleanup_job(env)
    assert job is not None and job.status == "succeeded"


async def test_content_retention_preserves_unknown_receipts(checkpoint_env):
    """Expired bodies and settled fees go; unknown charges and live runs stay."""

    env = checkpoint_env
    run = await active_run(env, "content")
    live = await active_run(env, "content-live")
    answer_id = await answer_call(env, run.run_id, "kept answer")
    await finish_run(
        env,
        run.lease,
        run.run_id,
        RunOutcome(status="answered", answer_call_id=answer_id),
    )

    old = datetime.now(UTC) - timedelta(days=91)
    future = datetime.now(UTC) + timedelta(days=1)
    async with env.factory.begin() as db:
        conversation = Conversation(
            actor_id=env.actor.id,
            idempotency_key=uuid4().hex,
            input_hash="a" * 64,
        )
        db.add(conversation)
        await db.flush()
        stale_message = Message(
            conversation_id=conversation.id,
            ordinal=0,
            role="user",
            body="expired question",
            body_bytes=len(b"expired question"),
            created_at=old,
        )
        recent_message = Message(
            conversation_id=conversation.id,
            ordinal=1,
            role="user",
            body="recent question",
            body_bytes=len(b"recent question"),
        )
        expired_call = ToolCall(
            run_id=run.run_id,
            ordinal=2,
            kind="retrieval",
            arguments={"version": 1},
            status="succeeded",
            completed_at=old,
            expires_at=old,
        )
        fresh_call = ToolCall(
            run_id=run.run_id,
            ordinal=3,
            kind="retrieval",
            arguments={"version": 1},
            status="succeeded",
            completed_at=old,
            expires_at=future,
        )
        live_call = ToolCall(
            run_id=live.run_id,
            ordinal=0,
            kind="retrieval",
            arguments={"version": 1},
            status="succeeded",
            completed_at=old,
            expires_at=old,
        )
        settled = RequestReceipt(
            run_id=run.run_id,
            kind="model",
            ordinal=0,
            request_hash="b" * 64,
            parameter_version="params-v1",
            status="settled",
            input_cap=1000,
            output_cap=100,
            amount=Decimal("0.100000"),
            currency="USD",
            pricing_version="test-pricing-v1",
            settled_at=old,
        )
        unknown = RequestReceipt(
            run_id=run.run_id,
            kind="model",
            ordinal=1,
            request_hash="c" * 64,
            parameter_version="params-v1",
            status="unknown",
            input_cap=1000,
            output_cap=100,
            amount=Decimal("0.100000"),
            currency="USD",
            pricing_version="test-pricing-v1",
        )
        reserved = RequestReceipt(
            run_id=run.run_id,
            kind="model",
            ordinal=2,
            request_hash="d" * 64,
            parameter_version="params-v1",
            status="reserved",
            input_cap=1000,
            output_cap=100,
        )
        db.add_all(
            [
                stale_message,
                recent_message,
                expired_call,
                fresh_call,
                live_call,
                settled,
                unknown,
                reserved,
            ]
        )
        await db.flush()
        identifiers = {
            "messages": (stale_message.id, recent_message.id),
            "calls": (expired_call.id, fresh_call.id, live_call.id),
            "receipts": (settled.id, unknown.id, reserved.id),
        }

    result = await sweep_once(env)
    assert result.tool_calls_reclaimed == 1
    assert result.messages_reclaimed == 1
    assert result.receipts_reclaimed == 1

    stale_id, recent_id = identifiers["messages"]
    expired_id, fresh_id, live_id = identifiers["calls"]
    settled_id, unknown_id, reserved_id = identifiers["receipts"]
    async with env.factory() as db:
        assert await db.get(Message, stale_id) is None
        assert await db.get(Message, recent_id) is not None
        assert await db.get(ToolCall, expired_id) is None
        assert await db.get(ToolCall, fresh_id) is not None
        # An active run keeps even its expired intermediate result.
        assert await db.get(ToolCall, live_id) is not None
        assert await db.get(RequestReceipt, settled_id) is None
        assert (await db.get(RequestReceipt, unknown_id)).status == "unknown"
        assert (await db.get(RequestReceipt, reserved_id)).status == "reserved"
