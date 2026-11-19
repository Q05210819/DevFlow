"""Private chat transport: old messages are visible, never resent as context.

Real PostgreSQL, policy, graph, official saver, HTTP and SSE. Only the model
transport is scripted, so this proves route authorization, per-request context
isolation and citation freshness, never model quality or provider acceptance.
"""

import hashlib
import json
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import func, text
from support.chat_env import chat_env as chat_env  # noqa: PLC0414
from support.chat_graph_env import ScriptedProvider, build, claim, publish, snapshot
from support.checkpoint_env import (
    checkpoint_env as checkpoint_env,  # noqa: PLC0414
)
from support.index_env import NEXT_SHA, SHA
from support.job_env import job_env as job_env  # noqa: PLC0414
from support.knowledge_env import knowledge_env as knowledge_env  # noqa: PLC0414
from support.knowledge_env import repo_env as repo_env  # noqa: PLC0414
from support.repository_env import browser
from support.sse import stream

from devflow.auth.models import Session

pytestmark = pytest.mark.asyncio


async def session_for(env, user):
    raw = f"synthetic-chat-session-{uuid4()}"
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


async def headers(env, client, *, key=None):
    current = (await client.get("/api/v1/session")).json()
    value = {
        "Origin": env.settings.public_base_url,
        "X-CSRF-Token": current["csrf_token"],
    }
    if key is not None:
        value["Idempotency-Key"] = key
    return value


def selection(env, sha=SHA):
    return {
        "selected_repository_ids": [str(env.repository.id)],
        "indexes": [
            {"repository_id": str(env.repository.id), "sha": sha, "target": "target"}
        ],
    }


def event_data(body):
    lines = body.strip().splitlines()
    assert lines[0].startswith("id: ")
    assert lines[1] == "event: job.status"
    payload = json.loads(lines[2].removeprefix("data: "))
    assert set(payload) == {"status", "attempt", "error_code"}
    return int(lines[0].removeprefix("id: ")), payload


def document_line(start_line):
    """The stored fixture body is a title line followed by a blank line."""
    return "# indexed document\n" if start_line == 1 else "\n"


def rendered(call):
    """Read the exact dispatched bytes, the same bytes the budget hashed.

    Recovery rebuilds a prepared call from storage without LangChain objects, so
    the serialized body - not ``messages`` - is the only authoritative context.
    """
    payload = json.loads(call.body)
    return "\n".join(
        json.dumps(message, ensure_ascii=False) for message in payload["messages"]
    )


def messages_path(conversation_id):
    return f"/api/v1/conversations/{conversation_id}/messages"


def citation_path(conversation_id, message_id, citation_id):
    return (
        f"/api/v1/conversations/{conversation_id}/messages/"
        f"{message_id}/citations/{citation_id}"
    )


async def create_conversation(env, client, key):
    response = await client.post(
        "/api/v1/conversations",
        json={"selected_repository_ids": [str(env.repository.id)]},
        headers=await headers(env, client, key=key),
    )
    assert response.status_code == 201
    assert response.headers["cache-control"] == "private, no-store"
    return response.json()


async def submit_message(env, client, conversation_id, question, key, *, sha=SHA):
    response = await client.post(
        messages_path(conversation_id),
        json={"selection": selection(env, sha), "question": question},
        headers=await headers(env, client, key=key),
    )
    assert response.status_code == 202
    assert response.headers["cache-control"] == "private, no-store"
    return response.json()


async def run_once(env, built):
    lease = await claim(env)
    await built.handler.run(lease)
    return lease


async def test_chat_routes_history_not_context(checkpoint_env):
    env = checkpoint_env
    provider = ScriptedProvider(["answer", "answer"])
    built = build(env, provider)
    first, _ = await publish(env, built.index)

    async with browser(env) as (client, app, raw):
        client.cookies.set("devflow_session", raw)
        conversation = await create_conversation(env, client, "route-create")
        conversation_id = conversation["id"]
        assert conversation["archived"] is False
        assert conversation["selected_repository_ids"] == [str(env.repository.id)]

        one = await submit_message(
            env, client, conversation_id, "first question", "route-run-1"
        )
        lease = await run_once(env, built)
        assert str(lease.job_id) == one["job_id"]

        history = await client.get(messages_path(conversation_id))
        assert history.status_code == 200
        assert history.headers["cache-control"] == "private, no-store"
        page = history.json()
        assert [message["role"] for message in page["messages"]] == [
            "assistant",
            "user",
        ]
        assert [message["body"] for message in page["messages"]] == [
            "answer 0",
            "first question",
        ]
        assert [message["ordinal"] for message in page["messages"]] == [1, 0]
        assert page["next_cursor"] is None
        assert page["messages"][1]["citations"] == []
        answer = page["messages"][0]
        # Overlapping chunks of one document collapse to a single evidence block;
        # either chunk may win the tie, so only the document identity is fixed.
        assert len(answer["citations"]) == 1
        citation = answer["citations"][0]
        assert citation["path"] == first.document_paths[0]
        assert citation["start_line"] in (1, 2)
        old_preview = await client.get(
            citation_path(conversation_id, answer["id"], citation["citation_id"])
        )
        assert old_preview.status_code == 200
        preview = old_preview.json()
        assert preview["text"] == document_line(citation["start_line"])
        assert preview["identity"]["path"] == first.document_paths[0]
        assert preview["is_current"] is True

        # A newer snapshot makes the stored citation historical, still readable.
        second, _ = await publish(env, built.index, sha=NEXT_SHA)
        historical = await client.get(
            citation_path(conversation_id, answer["id"], citation["citation_id"])
        )
        assert historical.status_code == 200
        assert historical.json()["is_current"] is False
        assert historical.json()["text"] == document_line(citation["start_line"])

        two = await submit_message(
            env,
            client,
            conversation_id,
            "second question",
            "route-run-2",
            sha=NEXT_SHA,
        )
        await run_once(env, built)

        # History stays visible in the UI, but the second request carries only
        # the current question and this round's evidence.
        sent = rendered(provider.invoked[1])
        assert "second question" in sent
        assert second.document_paths[0] in sent
        assert "first question" not in sent
        assert "answer 0" not in sent
        assert first.document_paths[0] not in sent

        latest = (await client.get(messages_path(conversation_id))).json()
        newest = latest["messages"][0]
        assert newest["body"] == "answer 1"
        assert [message["body"] for message in latest["messages"]] == [
            "answer 1",
            "second question",
            "answer 0",
            "first question",
        ]
        new_citation = newest["citations"][0]
        assert new_citation["path"] == second.document_paths[0]
        new_preview = await client.get(
            citation_path(conversation_id, newest["id"], new_citation["citation_id"])
        )
        assert new_preview.status_code == 200
        assert new_preview.json()["is_current"] is True
        assert new_preview.json()["identity"]["path"] == second.document_paths[0]

        first_page = (
            await client.get(messages_path(conversation_id), params={"limit": 2})
        ).json()
        assert [message["body"] for message in first_page["messages"]] == [
            "answer 1",
            "second question",
        ]
        assert first_page["next_cursor"] == "2"
        second_page = (
            await client.get(
                messages_path(conversation_id),
                params={"limit": 2, "cursor": first_page["next_cursor"]},
            )
        ).json()
        assert [message["body"] for message in second_page["messages"]] == [
            "answer 0",
            "first question",
        ]
        assert second_page["next_cursor"] is None

        # Progress is streamed; the final body is only ever read from history.
        async with stream(
            app, f"/api/v1/jobs/{two['job_id']}/events?after=0", raw
        ) as response:
            assert response.status == 200
            assert response.headers["content-type"].startswith("text/event-stream")
            assert response.headers["cache-control"] == "no-store"
            assert response.headers["x-accel-buffering"] == "no"
            received = [event_data(await response.body()) for _ in range(3)]
            assert [sequence for sequence, _ in received] == [1, 2, 3]
            assert [payload["status"] for _, payload in received] == [
                "queued",
                "running",
                "succeeded",
            ]
            assert [payload["attempt"] for _, payload in received] == [0, 1, 1]
            assert all(payload["error_code"] is None for _, payload in received)
            await response.finished()

        # Source access is not conversation access, even for a full member.
        intruder_raw = await session_for(env, await env.other_user(env.repository.id))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url=env.settings.public_base_url,
        ) as intruder:
            intruder.cookies.set("devflow_session", intruder_raw)
            denied = [
                await intruder.get(messages_path(conversation_id)),
                await intruder.get(
                    citation_path(
                        conversation_id, newest["id"], new_citation["citation_id"]
                    )
                ),
                await intruder.post(
                    messages_path(conversation_id),
                    json={
                        "selection": selection(env, NEXT_SHA),
                        "question": "intrusion",
                    },
                    headers=await headers(env, intruder, key="intrude-run"),
                ),
            ]
            for response in denied:
                assert response.status_code == 404
                assert response.headers["cache-control"] == "private, no-store"
                assert response.json()["error"]["code"] == "resource_not_found"
            assert (
                await intruder.get(f"/api/v1/jobs/{two['job_id']}")
            ).status_code == 404
            cancel = await intruder.post(
                f"/api/v1/jobs/{two['job_id']}/cancel",
                headers=await headers(env, intruder, key="intrude-cancel"),
            )
            assert cancel.status_code == 404
            async with stream(
                app, f"/api/v1/jobs/{two['job_id']}/events?after=0", intruder_raw
            ) as response:
                assert response.status == 404
                assert (
                    json.loads(await response.body())["error"]["code"]
                    == "resource_not_found"
                )

        # Revoking the only source revokes history, citation previews, new
        # submissions, the Job, cancellation and SSE replay.
        await env.revoke(env.repository.id)
        revoked = [
            await client.get(messages_path(conversation_id)),
            await client.get(
                citation_path(
                    conversation_id, newest["id"], new_citation["citation_id"]
                )
            ),
            await client.post(
                messages_path(conversation_id),
                json={"selection": selection(env, NEXT_SHA), "question": "revoked"},
                headers=await headers(env, client, key="revoked-run"),
            ),
        ]
        for response in revoked:
            assert response.status_code == 404
            assert response.headers["cache-control"] == "private, no-store"
            assert response.json()["error"]["code"] == "resource_not_found"
        assert (await client.get(f"/api/v1/jobs/{two['job_id']}")).status_code == 404
        revoked_cancel = await client.post(
            f"/api/v1/jobs/{two['job_id']}/cancel",
            headers=await headers(env, client, key="revoked-cancel"),
        )
        assert revoked_cancel.status_code == 404
        async with stream(
            app, f"/api/v1/jobs/{two['job_id']}/events?after=0", raw
        ) as response:
            assert response.status == 404
            assert (
                json.loads(await response.body())["error"]["code"]
                == "resource_not_found"
            )


async def test_chat_routes_cloud_unconfigured_keeps_local_search(checkpoint_env):
    env = checkpoint_env
    built = build(env, None)
    await publish(env, built.index)

    async with browser(env) as (client, _app, raw):
        client.cookies.set("devflow_session", raw)
        conversation = await create_conversation(env, client, "cloud-create")
        receipt = await submit_message(
            env,
            client,
            conversation["id"],
            "local evidence only",
            "cloud-run",
        )
        await run_once(env, built)

        run = await snapshot(env, receipt["run_id"])
        assert run.run.status == "failed"
        assert run.run.terminal_reason == "model_not_configured"
        assert [(call.ordinal, call.kind, call.status) for call in run.calls] == [
            (0, "retrieval", "succeeded")
        ]
        assert run.receipts == []
        assert run.job.status == "failed"
        assert run.job.error_code == "model_not_configured"
        # Local search really ran before the missing cloud configuration stopped
        # the run; no request was dispatched.
        assert built.index.calls
        assert built.provider is None

        history = await client.get(messages_path(conversation["id"]))
        assert history.status_code == 200
        assert [message["role"] for message in history.json()["messages"]] == ["user"]
        job = await client.get(f"/api/v1/jobs/{receipt['job_id']}")
        assert job.status_code == 200
        assert job.headers["cache-control"] == "no-store"
        assert job.json()["status"] == "failed"
        assert job.json()["error_code"] == "model_not_configured"
