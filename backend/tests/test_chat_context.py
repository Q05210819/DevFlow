"""Evidence-only context assembly, measured on the real serialized body.

The assembler reuses 3C evidence identities and never accepts conversation
history, logs or a widened scope. The 24 KiB budget is checked against the
bytes that are actually dispatched, not against a message count.
"""

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from langchain_core.messages import AIMessage, SystemMessage, ToolMessage

from devflow.chat.context import ContextAssembler, ToolExchange
from devflow.chat.provider import ChatProvider
from devflow.errors import AppError
from devflow.knowledge.citations import assign_ids, identity_digest
from devflow.knowledge.schemas import (
    ChunkLocation,
    CitationEntry,
    Evidence,
    EvidenceManifest,
    EvidenceSet,
    SourceIdentity,
)

pytestmark = pytest.mark.asyncio

REPO = uuid4()
OTHER = uuid4()
SHA = "a" * 40
REVISION = "b" * 64


def scope(*repositories):
    return SimpleNamespace(selected_repository_ids=tuple(repositories))


def make_evidence(
    *,
    text,
    document=None,
    repo=REPO,
    chunk=None,
    start=0,
    path="src/app.py",
):
    encoded = text.encode("utf-8")
    return Evidence(
        identity=SourceIdentity(
            repository_id=repo,
            source_repository_id=repo,
            dependency_repository_ids=(repo,),
            document_id=document or uuid4(),
            revision_hash=REVISION,
            workspace_id=uuid4(),
            sha=SHA,
            path=path,
        ),
        chunk_id=chunk or uuid4(),
        index_version_id=uuid4(),
        location=ChunkLocation(
            start_line=1,
            end_line=10,
            start_byte=start,
            end_byte=start + len(encoded),
        ),
        text=text,
        content_hash=hashlib.sha256(encoded).hexdigest(),
        rank=1,
    )


def evidence_set(items, *, selected=(REPO,)):
    ranked = tuple(replace(item, rank=rank) for rank, item in enumerate(items, start=1))
    return EvidenceSet(
        scope=scope(*selected),
        status="evidence",
        evidence=ranked,
        coverage=(),
    )


class Citations:
    """Deterministic 3C identity source; the DB lookup is 3C's concern."""

    def __init__(self):
        self.calls = 0

    async def manifest(self, evidence):
        self.calls += 1
        items = evidence.evidence
        ids = assign_ids(
            [identity_digest(item.identity, item.location) for item in items]
        )
        return EvidenceManifest(
            entries=tuple(
                CitationEntry(
                    citation_id=citation_id,
                    identity=item.identity,
                    location=item.location,
                    url=f"https://example.test/{citation_id}",
                    content_hash=item.content_hash,
                    rank=item.rank,
                )
                for item, citation_id in zip(items, ids, strict=True)
            )
        )


def reply_payload(content):
    return {
        "id": "req-1",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 11, "completion_tokens": 7},
    }


def valid_answer():
    return json.dumps(
        {
            "conclusion": "answer",
            "citation_ids": [],
            "insufficient_evidence": False,
            "next_steps": [],
        },
        separators=(",", ":"),
    )


class Capture:
    def __init__(self):
        self.requests = []

    async def __call__(self, request):
        self.requests.append(request)
        return httpx.Response(200, json=reply_payload(valid_answer()))


async def test_limit_counts_actual_json_body_including_tools_and_escaping():
    capture = Capture()
    async with httpx.AsyncClient(transport=httpx.MockTransport(capture)) as client:
        provider = ChatProvider("test-key", client)
        citations = Citations()
        assembler = ContextAssembler(citations, provider)

        # No complete evidence block fits: fail before any provider call.
        giant = make_evidence(text="中" * 20000)
        with pytest.raises(AppError) as error:
            await assembler.assemble("问题", evidence_set([giant]))
        assert error.value.code == "context_limit"
        assert capture.requests == []
        assert citations.calls == 1

        # Mandatory protocol alone over budget is also context_limit.
        with pytest.raises(AppError) as error:
            provider.prepare(
                [SystemMessage("x" * 30000)], [], EvidenceManifest(entries=())
            )
        assert error.value.code == "context_limit"
        assert capture.requests == []

        text = 'quote " and backslash \\ plus 多字节汉字' * 20
        current = evidence_set(
            [
                make_evidence(text=text, start=0),
                make_evidence(text="second block", start=1000),
            ]
        )
        prepared = await assembler.assemble('请解释 "引号" 和 \\ 反斜杠', current)
        assert len(prepared.body) <= 24576
        assert prepared.request_hash == hashlib.sha256(prepared.body).hexdigest()
        decoded = prepared.body.decode("utf-8")
        assert "多字节汉字" in decoded
        # Non-ASCII text is serialized as UTF-8, not as JSON escapes; the
        # limit therefore counts the same bytes the transport receives.
        assert "\\u591a" not in decoded

        reply = await provider.invoke(
            prepared, deadline=datetime.now(UTC) + timedelta(seconds=30)
        )
        assert reply.answer is not None
        assert len(capture.requests) == 1
        sent = capture.requests[0].content
        assert sent == prepared.body
        assert hashlib.sha256(sent).hexdigest() == prepared.request_hash
        assert len(sent) <= 24576
        wire = json.loads(sent)
        assert wire["tools"][0]["function"]["name"] == ("search_repository_evidence")
        assert wire["enable_thinking"] is False
        assert wire["max_completion_tokens"] == 1024
        assert "多字节汉字" in sent.decode("utf-8")
        # Quotes and backslashes are JSON-escaped on the wire, and those
        # escaped bytes are part of the checked budget.
        assert text in wire["messages"][-1]["content"]


async def test_second_context_prefers_new_and_drops_old_citations():
    capture = Capture()
    async with httpx.AsyncClient(transport=httpx.MockTransport(capture)) as client:
        provider = ChatProvider("test-key", client)
        citations = Citations()
        assembler = ContextAssembler(citations, provider)

        doc_a, doc_b, doc_c, doc_d = uuid4(), uuid4(), uuid4(), uuid4()
        first = make_evidence(text="NEW-ONE", document=doc_a, path="a.py", start=0)
        current = evidence_set(
            [
                first,
                make_evidence(text="NEW-TWO", document=doc_b, path="b.py", start=0),
                make_evidence(text="NEW-THREE", document=doc_c, path="c.py", start=0),
            ]
        )
        previous = evidence_set(
            [
                # Same chunk as the new rank-1 evidence: duplicate.
                make_evidence(
                    text="OLD-DUP",
                    document=doc_a,
                    path="a.py",
                    start=0,
                    chunk=first.chunk_id,
                ),
                # Second block of an already covered file, still legal.
                make_evidence(text="OLD-B2", document=doc_b, path="b.py", start=100),
                make_evidence(text="OLD-C2", document=doc_c, path="c.py", start=100),
                # Second block of A: now A has reached its two-block limit.
                make_evidence(text="OLD-A2", document=doc_a, path="a.py", start=100),
                # Would be the seventh block: dropped by the hard cap.
                make_evidence(text="OLD-DROPPED", document=doc_d, path="d.py", start=0),
                # Never authorized for this scope: filtered before packing.
                make_evidence(
                    text="UNSELECTED-OLD",
                    document=uuid4(),
                    repo=OTHER,
                    path="x.py",
                    start=0,
                ),
            ]
        )
        exchange = ToolExchange(
            tool_call_id="call_abc",
            query="search again",
            status="evidence",
            evidence_count=6,
        )

        prepared = await assembler.assemble(
            "question", current, previous=previous, tool_exchange=exchange
        )

        entries = prepared.manifest.entries
        assert [item.rank for item in entries] == list(range(1, 7))
        assert [item.identity.document_id for item in entries] == [
            doc_a,
            doc_b,
            doc_c,
            doc_b,
            doc_c,
            doc_a,
        ]
        per_file = {}
        for entry in entries:
            key = (entry.identity.document_id, entry.identity.revision_hash)
            per_file[key] = per_file.get(key, 0) + 1
        assert max(per_file.values()) <= 2

        decoded = prepared.body.decode("utf-8")
        for included in (
            "NEW-ONE",
            "NEW-TWO",
            "NEW-THREE",
            "OLD-B2",
            "OLD-C2",
            "OLD-A2",
        ):
            assert included in decoded
        for dropped in ("OLD-DUP", "OLD-DROPPED", "UNSELECTED-OLD"):
            assert dropped not in decoded
        assert all(entry.identity.repository_id != OTHER for entry in entries)

        calls = [item for item in prepared.messages if isinstance(item, AIMessage)]
        assert len(calls) == 1
        assert calls[0].tool_calls[0]["id"] == "call_abc"
        assert calls[0].tool_calls[0]["name"] == "search_repository_evidence"
        results = [item for item in prepared.messages if isinstance(item, ToolMessage)]
        assert len(results) == 1
        assert results[0].tool_call_id == "call_abc"
        assert "call_abc" in decoded


async def test_cloud_body_has_no_old_history_logs_or_unselected_source():
    capture = Capture()
    async with httpx.AsyncClient(transport=httpx.MockTransport(capture)) as client:
        provider = ChatProvider("test-key", client)
        assembler = ContextAssembler(Citations(), provider)

        current = evidence_set(
            [make_evidence(text="CURRENT-EVIDENCE", document=uuid4())]
        )
        previous = evidence_set(
            [
                make_evidence(
                    text="UNSELECTED-REPO-SENTINEL",
                    document=uuid4(),
                    repo=OTHER,
                    path="secret.py",
                    start=0,
                )
            ]
        )
        prepared = await assembler.assemble(
            "ONLY-THIS-QUESTION",
            current,
            previous=previous,
            tool_exchange=ToolExchange(
                tool_call_id="call_x",
                query="narrow query",
                status="evidence",
                evidence_count=1,
            ),
        )

        wire = json.loads(prepared.body)
        assert set(wire) == {
            "model",
            "stream",
            "messages",
            "max_completion_tokens",
            "tools",
            "tool_choice",
            "enable_thinking",
        }
        assert [message["role"] for message in wire["messages"]] == [
            "system",
            "user",
            "assistant",
            "tool",
            "user",
        ]
        assert wire["messages"][1]["content"] == "ONLY-THIS-QUESTION"
        assert "CURRENT-EVIDENCE" in wire["messages"][4]["content"]
        decoded = prepared.body.decode("utf-8")
        for forbidden in (
            "UNSELECTED-REPO-SENTINEL",
            "OLD-HISTORY-SENTINEL",
            "CI-LOG-SENTINEL",
        ):
            assert forbidden not in decoded
