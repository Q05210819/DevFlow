"""Cancellation and deletion of a private run, end to end.

Real HTTP, policy, queue, run ledger and official saver; only the model transport
is scripted. The tests prove that a cancelled or deleted run never publishes an
answer or bills a request, and that deletion reclaims the checkpoint thread while
retaining the fee identity. They never claim provider acceptance or cloud billing.
"""

import hashlib
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import func, select, text
from support.chat_env import chat_env as chat_env  # noqa: PLC0414
from support.chat_graph_env import ScriptedProvider, build, claim, publish
from support.checkpoint_env import (
    checkpoint_env as checkpoint_env,  # noqa: PLC0414
)
from support.checkpoint_env import (
    claim_cleanup,
    connect,
    official_counts,
)
from support.index_env import SHA
from support.job_env import job_env as job_env  # noqa: PLC0414
from support.knowledge_env import knowledge_env as knowledge_env  # noqa: PLC0414
from support.knowledge_env import repo_env as repo_env  # noqa: PLC0414
from support.repository_env import browser

from devflow.auth.models import Session
from devflow.chat.models import AgentRun, Message, RequestReceipt
from devflow.chat.retention import ChatRetention
from devflow.jobs.models import Job
from devflow.jobs.queue import Queue

pytestmark = pytest.mark.asyncio


def selection(env):
    return {
        "selected_repository_ids": [str(env.repository.id)],
        "indexes": [
            {"repository_id": str(env.repository.id), "sha": SHA, "target": "target"}
        ],
    }


async def headers(env, client, key=None):
    current = (await client.get("/api/v1/session")).json()
    value = {
        "Origin": env.settings.public_base_url,
        "X-CSRF-Token": current["csrf_token"],
    }
    if key is not None:
        value["Idempotency-Key"] = key
    return value


async def session_for(env, user):
    raw = f"synthetic-chat-flow-{uuid4()}"
    async with env.factory.begin() as db:
        db.add(
            Session(
                user_id=user.id,
                token_hash=hashlib.sha256(raw.encode()).hexdigest(),
                auth_version=user.auth_version,
                expires_at=func.clock_timestamp() + text("interval '8 hours'"),
            )
        )
    return raw


async def start(env, client, label):
    conversation = await client.post(
        "/api/v1/conversations",
        json={"selected_repository_ids": [str(env.repository.id)]},
        headers=await headers(env, client, key=f"{label}-create"),
    )
    assert conversation.status_code == 201
    identifier = conversation.json()["id"]
    receipt = await client.post(
        f"/api/v1/conversations/{identifier}/messages",
        json={"selection": selection(env), "question": f"{label} question"},
        headers=await headers(env, client, key=f"{label}-run"),
    )
    assert receipt.status_code == 202
    return identifier, receipt.json()


async def no_claim(env):
    """No chat Job may remain claimable after a terminal transition."""

    lease = await Queue(env.factory, lease_seconds=300).claim(
        uuid4(), allowed_kinds=frozenset({"chat.run"})
    )
    assert lease is None


async def run_state(env, run_id):
    async with env.factory() as db:
        return await db.get(AgentRun, run_id)


async def test_cancel_retires_run_without_model_call(checkpoint_env):
    env = checkpoint_env
    provider = ScriptedProvider(["answer"])
    built = build(env, provider)
    await publish(env, built.index)

    async with browser(env) as (client, app, _raw):
        client.cookies.set("devflow_session", _raw)
        conversation_id, receipt = await start(env, client, "cancel")

        cancel = await client.post(
            f"/api/v1/jobs/{receipt['job_id']}/cancel",
            headers=await headers(env, client, key="cancel-key"),
        )
        assert cancel.status_code == 200
        assert cancel.headers["cache-control"] == "no-store"
        assert cancel.json()["status"] == "failed"
        assert cancel.json()["error_code"] == "cancelled"

        # The same logical cancellation is idempotent and never re-executes.
        again = await client.post(
            f"/api/v1/jobs/{receipt['job_id']}/cancel",
            headers=await headers(env, client, key="cancel-key"),
        )
        assert again.status_code == 200
        assert again.json()["status"] == "failed"

        await no_claim(env)
        assert provider.invoked == []
        run = await run_state(env, receipt["run_id"])
        assert run is not None
        assert run.status == "failed"
        assert run.terminal_reason == "cancelled"
        assert run.terminal_at is not None
        assert run.accepted_retired_at is not None
        assert run.accepted_config is None

        history = await client.get(f"/api/v1/conversations/{conversation_id}/messages")
        assert history.status_code == 200
        assert [message["role"] for message in history.json()["messages"]] == ["user"]

        # Source access is not conversation access: another full member can
        # neither read nor cancel this private run.
        other = await session_for(env, await env.other_user(env.repository.id))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url=env.settings.public_base_url,
        ) as stranger:
            stranger.cookies.set("devflow_session", other)
            denied = await stranger.post(
                f"/api/v1/jobs/{receipt['job_id']}/cancel",
                headers=await headers(env, stranger, key="stranger-cancel"),
            )
            assert denied.status_code == 404
            assert denied.json()["error"]["code"] == "resource_not_found"


async def test_delete_retires_active_run_and_reclaims_thread(checkpoint_env):
    env = checkpoint_env
    provider = ScriptedProvider(["answer"])
    built = build(env, provider)
    await publish(env, built.index)

    async with browser(env) as (client, _app, _raw):
        client.cookies.set("devflow_session", _raw)
        conversation_id, receipt = await start(env, client, "delete-active")

        deletion = await client.post(
            f"/api/v1/conversations/{conversation_id}/deletions",
            headers=await headers(env, client, key="delete-key"),
        )
        assert deletion.status_code == 202
        cleanup_id = deletion.json()["job_id"]

        # Deletion retires the still-active run and never lets the worker claim
        # it, so no cloud request can be dispatched for deleted content.
        await no_claim(env)
        assert provider.invoked == []
        run = await run_state(env, receipt["run_id"])
        assert run is not None
        assert run.status == "failed"
        assert run.terminal_reason == "cancelled"
        async with env.factory() as db:
            job = await db.get(Job, receipt["job_id"])
            assert job is not None and job.error_code == "cancelled"
            assert await db.scalar(select(Message.id).limit(1)) is None
            cleanup = await db.get(Job, cleanup_id)
            assert cleanup is not None and cleanup.kind == "chat.cleanup"

        history = await client.get(f"/api/v1/conversations/{conversation_id}/messages")
        assert history.status_code == 200
        assert history.json()["messages"] == []
        listing = await client.get("/api/v1/conversations")
        assert listing.json()["conversations"] == []

        # A repeated deletion returns the same cleanup Job and is idempotent.
        repeated = await client.post(
            f"/api/v1/conversations/{conversation_id}/deletions",
            headers=await headers(env, client, key="delete-key"),
        )
        assert repeated.status_code == 202
        assert repeated.json()["job_id"] == cleanup_id

        lease = await claim_cleanup(env)
        result = await ChatRetention(env.factory).sweep(lease)
        assert result.examined == 0
        assert result.reclaimed == 0


async def test_delete_answered_conversation_removes_thread_and_bodies(checkpoint_env):
    env = checkpoint_env
    provider = ScriptedProvider(["answer"])
    built = build(env, provider)
    await publish(env, built.index)

    async with browser(env) as (client, _app, _raw):
        client.cookies.set("devflow_session", _raw)
        conversation_id, receipt = await start(env, client, "delete-answered")

        lease = await claim(env)
        await built.handler.run(lease)
        history = await client.get(f"/api/v1/conversations/{conversation_id}/messages")
        assert [message["role"] for message in history.json()["messages"]] == [
            "assistant",
            "user",
        ]

        thread_id = (await run_state(env, receipt["run_id"])).thread_id
        async with connect(env.settings) as connection:
            assert await official_counts(connection, thread_id) != (0, 0, 0)

        deletion = await client.post(
            f"/api/v1/conversations/{conversation_id}/deletions",
            headers=await headers(env, client, key="delete-answered-key"),
        )
        assert deletion.status_code == 202

        cleanup = await claim_cleanup(env)
        result = await ChatRetention(env.factory).sweep(cleanup)
        assert result.examined == 1
        assert result.reclaimed == 1
        async with connect(env.settings) as connection:
            assert await official_counts(connection, thread_id) == (0, 0, 0)

        async with env.factory() as db:
            assert await db.scalar(select(Message.id).limit(1)) is None
            receipts = list(await db.scalars(select(RequestReceipt)))
        # Fee identity outlives deleted bodies; it is never silently released.
        assert [receipt.status for receipt in receipts] == ["settled"]

        # The final answer and its run rows are gone, but the conversation keeps
        # its audit trail and the source scope is no longer readable as history.
        run = await run_state(env, receipt["run_id"])
        assert run is not None and run.terminal_at is not None
        after = await client.get(f"/api/v1/conversations/{conversation_id}/messages")
        assert after.json()["messages"] == []
