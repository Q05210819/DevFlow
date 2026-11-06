"""Private conversations, one durable active run, and participating-source scopes.

Real PostgreSQL, policy, Jobs and SSE. No model, GitHub or Milvus call is made;
this proves durable metadata and authorization, never answer quality.
"""

import asyncio
import json
from uuid import uuid4

import pytest
from sqlalchemy import func, select, update
from support.chat_env import chat_env as chat_env  # noqa: PLC0414
from support.job_env import job_env as job_env  # noqa: PLC0414
from support.knowledge_env import knowledge_env as knowledge_env  # noqa: PLC0414
from support.knowledge_env import repo_env as repo_env  # noqa: PLC0414
from support.repository_env import browser
from support.sse import stream

from devflow.auth.models import Session
from devflow.chat.models import AgentRun, Conversation, ConversationRepository, Message
from devflow.chat.schemas import RunReceipt
from devflow.errors import AppError
from devflow.jobs.events import JobEventService
from devflow.jobs.models import Job
from devflow.knowledge.schemas import IndexSelection, QuerySelection

pytestmark = pytest.mark.asyncio

SHA = "a" * 40


def selection(*repository_ids, indexed=None):
    """Select every source; only ``indexed`` repositories carry an index."""
    ids = tuple(repository_ids)
    indexed = tuple(indexed if indexed is not None else (ids[0],))
    return QuerySelection(
        selected_repository_ids=ids,
        indexes=tuple(
            IndexSelection(repository_id=repository_id, sha=SHA, target="target")
            for repository_id in indexed
        ),
    )


def event_data(body):
    lines = body.strip().splitlines()
    assert lines[1] == "event: job.status"
    payload = json.loads(lines[2].removeprefix("data: "))
    assert set(payload) == {"status", "attempt", "error_code"}
    return int(lines[0].removeprefix("id: ")), payload


async def test_conversation_is_private_and_scope_immutable(chat_env):
    env = chat_env
    fork_id = (await env.fork()).id

    view = await env.chat.create(
        env.actor, (env.repository.id, fork_id), "chat-create-0"
    )
    assert view.selected_repository_ids == (env.repository.id, fork_id)
    assert view.archived is False
    async with env.factory() as db:
        assert (
            await db.scalar(
                select(func.count()).select_from(Job).where(Job.kind == "chat.run")
            )
        ) == 0

    # A logical create key replays the same conversation regardless of order,
    # but never silently accepts a different source set.
    replay = await env.chat.create(
        env.actor, (fork_id, env.repository.id), "chat-create-0"
    )
    assert replay.id == view.id
    with pytest.raises(AppError) as error:
        await env.chat.create(env.actor, (env.repository.id,), "chat-create-0")
    assert error.value.code == "idempotency_conflict"

    narrow = await env.chat.create(env.actor, (env.repository.id,), "chat-narrow-0")

    for bad in ((), (env.repository.id, env.repository.id)):
        with pytest.raises(AppError) as error:
            await env.chat.create(env.actor, bad, f"chat-bad-{len(bad)}")
        assert error.value.code == "validation_error"

    # Ownership is checked before any source lookup, so an intruder learns
    # nothing and cannot even cause an upstream authorization call.
    other = await env.other_user(env.repository.id)
    admin = await env.other_user(github_id=44, role="admin")
    env.calls.clear()
    for actor in (other, admin):
        for action in (
            lambda actor=actor: env.chat.history(actor, view.id),
            lambda actor=actor: env.chat.authorize(actor, view.id, fresh=False),
            lambda actor=actor: env.chat.submit(
                actor,
                view.id,
                selection(env.repository.id),
                "intrusion",
                f"intrude-{actor.id}",
            ),
        ):
            with pytest.raises(AppError) as error:
                await action()
            assert error.value.status == 404
        with pytest.raises(AppError) as error:
            await env.chat.history(actor, uuid4())
        assert error.value.status == 404
    assert env.calls == []

    # A different selected set is a conflict, never a silent scope rewrite.
    with pytest.raises(AppError) as error:
        await env.chat.submit(
            env.actor, view.id, selection(env.repository.id), "subset", "scope-subset"
        )
    assert error.value.code == "scope_mismatch"
    with pytest.raises(AppError) as error:
        await env.chat.submit(
            env.actor,
            narrow.id,
            selection(env.repository.id, fork_id),
            "superset",
            "scope-superset",
        )
    assert error.value.code == "scope_mismatch"
    async with env.factory() as db:
        members = list(
            await db.scalars(
                select(ConversationRepository.repository_id)
                .where(ConversationRepository.conversation_id == view.id)
                .order_by(ConversationRepository.ordinal)
            )
        )
    assert members == [env.repository.id, fork_id]

    # A fork that only participates in authorization needs no index of its own.
    receipt = await env.chat.submit(
        env.actor,
        view.id,
        selection(env.repository.id, fork_id, indexed=(env.repository.id,)),
        "what is in both sources?",
        "chat-run-0",
    )
    async with env.factory() as db:
        run = await db.get(AgentRun, receipt.run_id)
        assert run.conversation_id == view.id
        assert run.job_id == receipt.job_id
        assert run.status == "queued" and run.terminal_at is None
        assert run.selection["selected_repository_ids"] == [
            str(env.repository.id),
            str(fork_id),
        ]
        assert [item["repository_id"] for item in run.selection["indexes"]] == [
            str(env.repository.id)
        ]
        job = await db.get(Job, receipt.job_id)
        assert job.kind == "chat.run"
        assert job.requested_by == env.actor.id
        assert job.input == {
            "version": 1,
            "run_id": str(receipt.run_id),
            "conversation_id": str(view.id),
            "message_id": str(receipt.message_id),
        }
        assert "what is in both" not in json.dumps(job.input)
        message = await db.get(Message, receipt.message_id)
        assert message.conversation_id == view.id
        assert message.run_id == receipt.run_id
        assert message.role == "user" and message.ordinal == 0
        assert message.body == "what is in both sources?"


async def test_one_active_run_and_durable_counts(chat_env):
    env = chat_env
    view = await env.chat.create(env.actor, (env.repository.id,), "count-create")
    chosen = selection(env.repository.id)

    async def submit(key):
        return await env.chat.submit(env.actor, view.id, chosen, f"question {key}", key)

    outcomes = await asyncio.gather(
        submit("race-a"), submit("race-b"), return_exceptions=True
    )
    successes = [outcome for outcome in outcomes if isinstance(outcome, RunReceipt)]
    failures = [outcome for outcome in outcomes if isinstance(outcome, AppError)]
    assert len(successes) == 1 and len(failures) == 1
    assert failures[0].code == "run_in_progress" and failures[0].status == 409
    receipt = successes[0]
    winning = "race-a" if outcomes[0] is receipt else "race-b"

    replay = await env.chat.submit(
        env.actor, view.id, chosen, f"question {winning}", winning
    )
    assert replay == receipt
    with pytest.raises(AppError) as error:
        await env.chat.submit(env.actor, view.id, chosen, "different", winning)
    assert error.value.code == "idempotency_conflict"

    async def mark_terminal(run_id):
        async with env.factory.begin() as db:
            now = await db.scalar(select(func.clock_timestamp()))
            run = await db.get(AgentRun, run_id)
            run.status = "failed"
            run.terminal_reason = "validation_error"
            run.terminal_at = now

    await mark_terminal(receipt.run_id)
    second = await env.chat.submit(env.actor, view.id, chosen, "second", "second-key")
    await mark_terminal(second.run_id)

    # One user message per run; a bounded assistant answer must still fit.
    async with env.factory.begin() as db:
        for ordinal in range(2, 198):
            db.add(
                Message(
                    conversation_id=view.id,
                    ordinal=ordinal,
                    role="assistant",
                    body="x",
                    body_bytes=1,
                )
            )
    fill = await env.chat.submit(env.actor, view.id, chosen, "fill", "fill-key")
    async with env.factory() as db:
        count = await db.scalar(
            select(func.count())
            .select_from(Message)
            .where(Message.conversation_id == view.id)
        )
        assert count == 199
    await mark_terminal(fill.run_id)
    with pytest.raises(AppError) as error:
        await env.chat.submit(env.actor, view.id, chosen, "overflow", "overflow-key")
    assert error.value.code == "storage_exhausted"

    # Archiving does not make room and refuses new runs.
    async with env.factory.begin() as db:
        conversation = await db.get(Conversation, view.id)
        conversation.archived_at = await db.scalar(select(func.clock_timestamp()))
    with pytest.raises(AppError) as error:
        await env.chat.submit(env.actor, view.id, chosen, "archived", "archived-key")
    assert error.value.code == "conversation_archived"

    # The conversation cap counts archived rows too; replay still works at cap.
    cap = await env.other_user()
    async with env.factory.begin() as db:
        for index in range(99):
            db.add(
                Conversation(
                    actor_id=cap.id,
                    idempotency_key=f"seed-{index}",
                    input_hash="a" * 64,
                )
            )
    hundredth = await env.chat.create(cap, (env.repository.id,), "cap-99")
    with pytest.raises(AppError) as error:
        await env.chat.create(cap, (env.repository.id,), "cap-100")
    assert error.value.code == "storage_exhausted"
    async with env.factory.begin() as db:
        await db.execute(
            update(Conversation)
            .where(Conversation.actor_id == cap.id)
            .values(archived_at=func.clock_timestamp())
        )
    with pytest.raises(AppError) as error:
        await env.chat.create(cap, (env.repository.id,), "cap-101")
    assert error.value.code == "storage_exhausted"
    replay_cap = await env.chat.create(cap, (env.repository.id,), "cap-99")
    assert replay_cap.id == hundredth.id


async def test_all_participating_scopes_protect_history_job_and_sse(chat_env):
    env = chat_env
    fork_id = (await env.fork()).id
    view = await env.chat.create(
        env.actor, (env.repository.id, fork_id), "privacy-create"
    )
    chosen = selection(env.repository.id, fork_id, indexed=(env.repository.id,))
    receipt = await env.chat.submit(
        env.actor, view.id, chosen, "cite both sources", "privacy-run"
    )

    page = await env.chat.history(env.actor, view.id)
    assert [message.id for message in page.messages] == [receipt.message_id]
    assert page.messages[0].body == "cite both sources"
    assert page.next_cursor is None
    await env.jobs.get_visible(env.actor, receipt.job_id)
    events = await JobEventService(env.jobs).read_events(
        env.actor, receipt.job_id, after=0
    )
    assert [event.sequence for event in events] == [1]
    grant = await env.chat.authorize(env.actor, view.id, fresh=False)
    assert {epoch.repository_id for epoch in grant.repository_epochs} == {
        env.repository.id,
        fork_id,
    }

    # Full source access is not conversation access.
    other = await env.other_user(env.repository.id, fork_id)
    for action in (
        lambda: env.chat.history(other, view.id),
        lambda: env.chat.authorize(other, view.id, fresh=False),
        lambda: env.chat.submit(other, view.id, chosen, "intrusion", "privacy-intrude"),
        lambda: env.jobs.get_visible(other, receipt.job_id),
        lambda: JobEventService(env.jobs).read_events(other, receipt.job_id, after=0),
    ):
        with pytest.raises(AppError) as error:
            await action()
        assert error.value.status == 404

    async with browser(env) as (_, app, raw):
        async with stream(
            app, f"/api/v1/jobs/{receipt.job_id}/events?after=0", raw
        ) as response:
            assert response.status == 200
            sequence, payload = event_data(await response.body())
            assert sequence == 1 and payload["status"] == "queued"

        async with env.factory.begin() as db:
            await db.execute(update(Session).values(user_id=other.id))
        async with stream(
            app, f"/api/v1/jobs/{receipt.job_id}/events?after=0", raw
        ) as response:
            assert response.status == 404
            assert (
                json.loads(await response.body())["error"]["code"]
                == "resource_not_found"
            )
        async with env.factory.begin() as db:
            await db.execute(update(Session).values(user_id=env.actor.id))

        # Revoking any participating source, even an indexless fork, revokes
        # history, the Job, its events and every citation entry gate.
        await env.revoke(fork_id)
        for action in (
            lambda: env.chat.history(env.actor, view.id),
            lambda: env.chat.authorize(env.actor, view.id, fresh=False),
            lambda: env.chat.submit(
                env.actor, view.id, chosen, "after revoke", "privacy-revoked"
            ),
            lambda: env.jobs.get_visible(env.actor, receipt.job_id),
            lambda: JobEventService(env.jobs).read_events(
                env.actor, receipt.job_id, after=0
            ),
        ):
            with pytest.raises(AppError) as error:
                await action()
            assert error.value.status == 404
        async with stream(
            app, f"/api/v1/jobs/{receipt.job_id}/events?after=0", raw
        ) as response:
            assert response.status == 404
            assert (
                json.loads(await response.body())["error"]["code"]
                == "resource_not_found"
            )
