"""Finite ChatAgent control flow over real leases, retrieval and ledgers.

Only the provider transport is scripted. The counters, ToolCalls, receipts and
terminal transitions read back below are real PostgreSQL rows, so these tests
prove bounded control flow and durable recovery, never model quality, provider
acceptance or real cloud billing.
"""

import pytest
from sqlalchemy import func, select
from support.chat_env import chat_env as chat_env  # noqa: PLC0414
from support.chat_graph_env import (
    ScriptedProvider,
    build,
    claim,
    create_run,
    fail,
    publish,
    snapshot,
)
from support.checkpoint_env import (
    checkpoint_env as checkpoint_env,  # noqa: PLC0414
)
from support.job_env import job_env as job_env  # noqa: PLC0414
from support.knowledge_env import knowledge_env as knowledge_env  # noqa: PLC0414
from support.knowledge_env import repo_env as repo_env  # noqa: PLC0414

from devflow.errors import AppError
from devflow.jobs.models import Job

pytestmark = pytest.mark.asyncio


async def test_direct_retrieval_never_submits_child_job(checkpoint_env):
    env = checkpoint_env
    built = build(env, ScriptedProvider(["answer"]))
    await publish(env, built.index)
    receipt = await create_run(env, "direct")
    lease = await claim(env)

    contexts = []
    retrieve = built.retrieval.retrieve

    async def recording(context, question):
        contexts.append(context)
        return await retrieve(context, question)

    built.retrieval.retrieve = recording
    await built.handler.run(lease)

    state = await snapshot(env, receipt.run_id)
    assert len(contexts) == 1
    assert contexts[0].lease == lease
    assert contexts[0].scope.actor == env.actor
    assert contexts[0].scope.selected_repository_ids == (env.repository.id,)
    assert contexts[0].scope.grant.repository_epochs

    async with env.factory() as db:
        chat_jobs = await db.scalar(
            select(func.count()).select_from(Job).where(Job.kind == "chat.run")
        )
    assert chat_jobs == 1

    assert state.run.status == "succeeded"
    assert state.run.terminal_reason == "answered"
    assert state.run.terminal_at is not None
    assert state.run.accepted_config is None
    assert state.run.nodes_used == 4
    assert state.run.retrievals_used == 1
    assert state.run.model_requests_used == 1

    assert [(call.ordinal, call.kind, call.status) for call in state.calls] == [
        (0, "retrieval", "succeeded"),
        (1, "answer", "succeeded"),
    ]
    assert [message.role for message in state.messages] == ["user", "assistant"]
    answer = state.messages[1]
    assert answer.body == "answer 0"
    assert len(answer.manifest["entries"]) == 1
    assert [receipt.status for receipt in state.receipts] == ["settled"]
    assert state.job.status == "succeeded"
    assert state.job.error_code is None


async def test_two_calls_two_retrievals_eight_nodes(checkpoint_env):
    env = checkpoint_env
    built = build(env, ScriptedProvider(["tool", "tool"]))
    built.provider.prepare_fail_at = 1
    await publish(env, built.index)
    receipt = await create_run(env, "two-calls")

    first = await claim(env)
    with pytest.raises(AppError) as raised:
        await built.handler.run(first)
    assert raised.value.code == "service_unavailable"
    await fail(env, first, "service_unavailable")

    second = await claim(env)
    assert second.attempt == 2
    await built.handler.run(second)

    state = await snapshot(env, receipt.run_id)
    # Five node entries on the first attempt, three on the resumed attempt:
    # replaying a node still consumes the cumulative allowance.
    assert state.run.nodes_used == 8
    assert state.run.retrievals_used == 2
    assert state.run.model_requests_used == 2
    assert len(built.provider.invoked) == 2
    assert len(built.index.calls) == 2
    assert [row.status for row in state.receipts] == ["settled", "settled"]
    assert [call.kind for call in state.calls] == [
        "retrieval",
        "tool_request",
        "retrieval",
        "tool_request",
    ]
    assert state.run.status == "failed"
    assert state.run.terminal_reason == "answer_validation_failed"
    assert state.job.status == "failed"
    assert state.job.error_code == "answer_validation_failed"
    assert state.job.attempt == 2
    assert all(message.role == "user" for message in state.messages)


async def test_no_evidence_does_not_reserve_cloud(checkpoint_env):
    env = checkpoint_env
    built = build(env, ScriptedProvider(["answer"]))
    await publish(env, built.index, chunk_count=0)
    receipt = await create_run(env, "empty")
    lease = await claim(env)

    await built.handler.run(lease)

    state = await snapshot(env, receipt.run_id)
    assert state.run.status == "succeeded"
    assert state.run.terminal_reason == "no_evidence"
    assert state.run.nodes_used == 1
    assert state.run.retrievals_used == 1
    assert state.run.model_requests_used == 0
    assert state.receipts == []
    assert [message.role for message in state.messages] == ["user"]
    assert built.provider.invoked == []
    assert built.provider.prepare_calls == []
    assert len(built.index.calls) == 1
    assert state.job.status == "succeeded"


async def test_version_refresh_consumes_second_retrieval(checkpoint_env):
    env = checkpoint_env
    built = build(env, ScriptedProvider(["tool", "answer"]))
    await publish(env, built.index, generation=1)
    rotated = {}

    async def rotate(ordinal):
        if ordinal == 1:
            fixture, row = await publish(env, built.index, generation=2)
            rotated["fixture"], rotated["row"] = fixture, row

    built.inference.before_rerank = rotate
    receipt = await create_run(env, "refresh")
    lease = await claim(env)

    await built.handler.run(lease)

    state = await snapshot(env, receipt.run_id)
    assert state.run.status == "failed"
    assert state.run.terminal_reason == "version_changed"
    assert state.job.error_code == "version_changed"
    assert state.run.retrievals_used == 2
    assert state.run.model_requests_used == 1
    # One search before the rotation, one after the single allowed refreeze.
    assert len(built.index.calls) == 2
    assert len(built.provider.invoked) == 1
    assert state.run.scope["refreshed"] is True
    assert [item["index_version_id"] for item in state.run.scope["indexes"]] == [
        str(rotated["row"].id)
    ]

    more = next(call for call in state.calls if call.ordinal == 2)
    assert more.kind == "retrieval"
    assert more.status == "failed"
    assert more.result["error_code"] == "version_changed"
    assert state.run.terminal_at is not None
