"""Durable deadlines, replay boundaries and controlled terminal retirement.

Every deadline, receipt and terminal row below is read back from PostgreSQL.
The model transport is scripted, so nothing here claims real provider behavior.
"""

from datetime import timedelta

import pytest
from sqlalchemy import select, text, update
from support.chat_env import chat_env as chat_env  # noqa: PLC0414
from support.chat_graph_env import (
    PRICING,
    ScriptedProvider,
    build,
    claim,
    create_run,
    fail,
    publish,
    snapshot,
)
from support.checkpoint_env import (
    checkpoint,
    config,
    connect,
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

from devflow.chat.budget import BudgetService
from devflow.chat.checkpoints import FencedPostgresSaver
from devflow.chat.models import AgentRun, BudgetLedger, RequestReceipt
from devflow.chat.schemas import ProviderUsage, RunOutcome
from devflow.errors import AppError
from devflow.jobs.models import Job
from devflow.jobs.queue import Queue

pytestmark = pytest.mark.asyncio


async def expire(env, lease):
    async with env.factory.begin() as db:
        await db.execute(
            update(Job)
            .where(Job.id == lease.job_id)
            .values(lease_expires_at=text("clock_timestamp() - interval '1 second'"))
        )


async def test_queue_and_execution_deadlines_survive_restart(checkpoint_env):
    env = checkpoint_env
    built = build(env, ScriptedProvider(["answer"]))
    await publish(env, built.index)
    receipt = await create_run(env, "deadline")
    first = await claim(env)

    retrieve = built.retrieval.retrieve
    calls = 0

    async def outage(context, question):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise AppError("service_unavailable", 503, "Injected retrieval outage")
        return await retrieve(context, question)

    built.retrieval.retrieve = outage
    with pytest.raises(AppError) as raised:
        await built.handler.run(first)
    assert raised.value.code == "service_unavailable"

    after_first = await snapshot(env, receipt.run_id)
    assert after_first.run.status == "running"
    assert after_first.run.started_at is not None
    assert after_first.run.deadline - after_first.run.started_at == timedelta(
        seconds=180
    )
    assert after_first.run.queued_deadline - after_first.run.created_at == timedelta(
        minutes=10
    )
    execution_deadline = after_first.run.deadline

    await fail(env, first, "service_unavailable")
    second = await claim(env)
    assert second.attempt == 2
    await built.handler.run(second)

    final = await snapshot(env, receipt.run_id)
    assert final.run.status == "succeeded"
    assert final.run.terminal_reason == "answered"
    # A restart reuses the original window; it never extends it.
    assert final.run.started_at == after_first.run.started_at
    assert final.run.deadline == execution_deadline
    assert final.job.attempt == 2


async def test_known_call_reused_unknown_call_not_replayed(checkpoint_env):
    env = checkpoint_env
    built = build(env, ScriptedProvider(["answer"]))
    await publish(env, built.index)

    settle = built.budget.settle
    settle_calls = 0

    async def settle_then_crash(lease, receipt_id, usage):
        nonlocal settle_calls
        await settle(lease, receipt_id, usage)
        settle_calls += 1
        if settle_calls == 1:
            raise AppError("service_unavailable", 503, "Injected post-settle crash")

    built.budget.settle = settle_then_crash
    known = await create_run(env, "known")
    first = await claim(env)
    with pytest.raises(AppError) as raised:
        await built.handler.run(first)
    assert raised.value.code == "service_unavailable"
    await fail(env, first, "service_unavailable")
    second = await claim(env)
    await built.handler.run(second)

    known_state = await snapshot(env, known.run_id)
    assert known_state.run.status == "succeeded"
    assert known_state.run.terminal_reason == "answered"
    assert len(built.provider.invoked) == 1
    assert [receipt.status for receipt in known_state.receipts] == ["settled"]
    assert [message.role for message in known_state.messages] == [
        "user",
        "assistant",
    ]
    assert known_state.messages[1].body == "answer 0"

    # A dispatched call without a trusted result is never replayed: the
    # reservation stays held and the run terminates as unknown. The second
    # graph reuses the already published evidence index.
    crashy = build(env, ScriptedProvider(["answer"]), index=built.index)
    crashy.provider.fail_invoke_at = 0
    crashy.provider.invoke_error = AppError(
        "service_unavailable", 503, "Injected dispatch outage"
    )
    unknown = await create_run(env, "unknown")
    first = await claim(env)
    with pytest.raises(AppError) as raised:
        await crashy.handler.run(first)
    assert raised.value.code == "service_unavailable"
    await fail(env, first, "service_unavailable")
    second = await claim(env)
    assert second.attempt == 2
    await crashy.handler.run(second)

    unknown_state = await snapshot(env, unknown.run_id)
    assert unknown_state.run.status == "failed"
    assert unknown_state.run.terminal_reason == "model_call_outcome_unknown"
    assert unknown_state.job.error_code == "model_call_outcome_unknown"
    assert unknown_state.job.status == "failed"
    assert len(crashy.provider.invoked) == 1
    assert [receipt.status for receipt in unknown_state.receipts] == ["unknown"]
    reserved = unknown_state.ledgers[0].reserved
    assert reserved > 0


async def test_cancel_expiry_and_final_attempt_retire_atomically(checkpoint_env):
    env = checkpoint_env
    receipt = await create_run(env, "expire")
    lease = await claim(env)

    async with env.factory() as db:
        run = await db.scalar(select(AgentRun).where(AgentRun.id == receipt.run_id))
        thread_id = run.thread_id
    grant = await env.chat.access.verify(env.actor, [env.repository.id], fresh=True)

    async with connect(env.settings) as connection:
        saver = FencedPostgresSaver(
            connection, lease, receipt.run_id, grant, settings=env.settings
        )
        stored = checkpoint({"run_id": str(receipt.run_id)})
        await saver.aput(
            config(thread_id),
            stored,
            {"source": "loop", "step": 1},
            versions(stored),
        )
        before = await official_counts(connection, thread_id)
    assert before[0] >= 1

    budget = BudgetService(env.factory, env.settings.model_copy(update=PRICING))
    reservation = await budget.reserve(
        lease, receipt.run_id, "model:0", "a" * 64, "test-parameters"
    )
    await budget.mark_dispatched(lease, reservation.receipt_id)

    queue = Queue(env.factory, retry_first_seconds=0, retry_second_seconds=0)
    for attempt in (1, 2, 3):
        await expire(env, lease)
        assert await queue.recover_expired() >= 1
        if attempt < 3:
            lease = await claim(env)
            assert lease.attempt == attempt + 1

    final = await snapshot(env, receipt.run_id)
    assert final.run.status == "failed"
    assert final.run.terminal_reason == "lease_expired"
    assert final.run.terminal_at is not None
    assert final.run.accepted_retired_at is not None
    assert final.run.accepted_config is None
    assert final.job.status == "failed"
    assert final.job.error_code == "lease_expired"
    assert final.job.lease_owner is None
    assert final.job.attempt == 3

    async with connect(env.settings) as connection:
        stale_saver = FencedPostgresSaver(
            connection, lease, receipt.run_id, grant, settings=env.settings
        )
        stale = checkpoint({"run_id": str(receipt.run_id)})
        with pytest.raises(AppError) as raised:
            await stale_saver.aput(
                config(thread_id),
                stale,
                {"source": "loop", "step": 2},
                versions(stale),
            )
        assert raised.value.code == "lease_expired"
        assert await official_counts(connection, thread_id) == before

    with pytest.raises(AppError) as raised:
        await finish_run(
            env,
            lease,
            receipt.run_id,
            RunOutcome(status="failed", error_code="internal_error"),
        )
    assert raised.value.code == "lease_expired"
    assert [
        message.role for message in (await snapshot(env, receipt.run_id)).messages
    ] == ["user"]

    async with env.factory() as db:
        stored_receipt = await db.scalar(
            select(RequestReceipt).where(RequestReceipt.id == reservation.receipt_id)
        )
        ledger = await db.scalar(
            select(BudgetLedger).where(
                BudgetLedger.run_id == receipt.run_id,
                BudgetLedger.ledger_scope == "run",
            )
        )
        assert stored_receipt.status == "dispatched"
        reserved_before = ledger.reserved

    with pytest.raises(AppError) as raised:
        await budget.settle(
            lease,
            reservation.receipt_id,
            ProviderUsage(
                prompt_tokens=1000,
                completion_tokens=500,
                request_id="late-request",
                complete=True,
            ),
        )
    assert raised.value.code == "lease_expired"

    async with env.factory() as db:
        stored_receipt = await db.scalar(
            select(RequestReceipt).where(RequestReceipt.id == reservation.receipt_id)
        )
        ledger = await db.scalar(
            select(BudgetLedger).where(
                BudgetLedger.run_id == receipt.run_id,
                BudgetLedger.ledger_scope == "run",
            )
        )
        assert stored_receipt.status == "dispatched"
        assert ledger.reserved == reserved_before
