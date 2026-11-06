"""The fixed DashScope Chat Completions contract for private chat.

One authoritative serialization exit: :meth:`ChatProvider.prepare` builds and
hashes the exact bytes; :meth:`ChatProvider.invoke` re-checks them and sends
those bytes unchanged. There is no SDK retry, no Responses API fallback and no
tracing/search/cache-create parameter.
"""

import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
from langchain_core.messages import HumanMessage, SystemMessage

from devflow.chat.context import SEARCH_TOOL
from devflow.chat.provider import (
    CHAT_COMPLETIONS_URL,
    MAX_BODY_BYTES,
    MAX_COMPLETION_TOKENS,
    MODEL,
    ChatProvider,
    ProviderAnswerError,
)
from devflow.chat.schemas import AnswerDraft, ProviderUsage
from devflow.errors import AppError
from devflow.knowledge.schemas import (
    ChunkLocation,
    CitationEntry,
    EvidenceManifest,
    SourceIdentity,
)

pytestmark = pytest.mark.asyncio

CITATION = "a" * 16


def manifest():
    repo = uuid4()
    return EvidenceManifest(
        entries=(
            CitationEntry(
                citation_id=CITATION,
                identity=SourceIdentity(
                    repository_id=repo,
                    source_repository_id=repo,
                    dependency_repository_ids=(repo,),
                    document_id=uuid4(),
                    revision_hash="b" * 64,
                    workspace_id=None,
                    sha=None,
                    path=None,
                ),
                location=ChunkLocation(
                    start_line=1,
                    end_line=2,
                    start_byte=0,
                    end_byte=4,
                ),
                url=f"https://example.test/{CITATION}",
                content_hash="c" * 64,
                rank=1,
            ),
        )
    )


def messages():
    return [SystemMessage("fixed system prompt"), HumanMessage("question")]


def answer_content(**overrides):
    value = {
        "conclusion": "bounded answer",
        "citation_ids": [CITATION],
        "insufficient_evidence": False,
        "next_steps": ["read the caller"],
    }
    value.update(overrides)
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def chat_response(
    content=None,
    *,
    tool_calls=None,
    usage=True,
    request_id="req_1",
):
    message = {"role": "assistant", "content": content}
    finish = "stop"
    if tool_calls is not None:
        message["content"] = None
        message["tool_calls"] = tool_calls
        finish = "tool_calls"
    payload = {"choices": [{"index": 0, "message": message, "finish_reason": finish}]}
    if request_id is not None:
        payload["id"] = request_id
    if usage:
        payload["usage"] = {
            "prompt_tokens": 9,
            "completion_tokens": 4,
            "total_tokens": 13,
        }
    return payload


class Transport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    async def __call__(self, request):
        self.requests.append(request)
        item = self.responses.pop(0)
        if isinstance(item, httpx.Response):
            return item
        return httpx.Response(200, json=item)


def deadline(seconds=20):
    return datetime.now(UTC) + timedelta(seconds=seconds)


async def run_one(transport, provider_factory=None):
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        provider = ChatProvider("secret-key", client)
        prepared = provider.prepare(messages(), [SEARCH_TOOL], manifest())
        return await provider.invoke(prepared, deadline=deadline())


async def test_provider_fixed_chat_contract():
    transport = Transport([chat_response(answer_content())])
    reply = await run_one(transport)

    assert isinstance(reply.answer, AnswerDraft)
    assert reply.answer.citation_ids == (CITATION,)
    assert reply.tool_query is None
    assert reply.usage == ProviderUsage(
        prompt_tokens=9,
        completion_tokens=4,
        request_id="req_1",
        complete=True,
    )

    assert len(transport.requests) == 1
    request = transport.requests[0]
    assert str(request.url) == CHAT_COMPLETIONS_URL
    assert request.url.path.endswith("/chat/completions")
    assert request.method == "POST"
    assert request.headers["authorization"] == "Bearer secret-key"
    assert "responses" not in str(request.url).lower()

    body = json.loads(request.content)
    assert body["model"] == MODEL == "qwen3.8-max"
    assert body["stream"] is False
    assert body["enable_thinking"] is False
    assert body["max_completion_tokens"] == MAX_COMPLETION_TOKENS == 1024
    assert body["tool_choice"] == "auto"
    assert set(body) == {
        "model",
        "stream",
        "messages",
        "max_completion_tokens",
        "tools",
        "tool_choice",
        "enable_thinking",
    }
    assert len(body["tools"]) == 1
    function = body["tools"][0]["function"]
    assert function["name"] == "search_repository_evidence"
    assert set(function["parameters"]["properties"]) == {"query"}
    assert function["parameters"]["additionalProperties"] is False
    for forbidden in (
        "store",
        "search",
        "cache_creation",
        "metadata",
        "include",
        "tracing",
        "previous_response_id",
        "response_format",
    ):
        assert forbidden not in body

    # The SDK retry budget is zero: a single failed attempt is not re-sent.
    failing = Transport([httpx.Response(503, json={"error": "busy"})])
    async with httpx.AsyncClient(transport=httpx.MockTransport(failing)) as client:
        provider = ChatProvider("secret-key", client)
        with pytest.raises(AppError) as error:
            await provider.invoke(
                provider.prepare(messages(), [SEARCH_TOOL], manifest()),
                deadline=deadline(),
            )
    assert error.value.code == "service_unavailable"
    assert len(failing.requests) == 1


async def test_provider_tool_call_is_the_only_tool_protocol():
    tool_calls = [
        {
            "id": "call_1",
            "type": "function",
            "function": {
                "name": "search_repository_evidence",
                "arguments": json.dumps({"query": "find auth guard"}),
            },
        }
    ]
    reply = await run_one(Transport([chat_response(tool_calls=tool_calls)]))
    assert reply.answer is None
    assert reply.tool_query == "find auth guard"
    assert reply.tool_call_id == "call_1"
    assert reply.usage is not None


async def test_provider_rejects_invalid_tool_arguments_without_repair():
    bad_arguments = [
        json.dumps({"query": "x", "repository_id": str(uuid4())}),
        json.dumps({"query": ""}),
        "{not-json",
    ]
    for arguments in bad_arguments:
        transport = Transport(
            [
                chat_response(
                    tool_calls=[
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": "search_repository_evidence",
                                "arguments": arguments,
                            },
                        }
                    ]
                )
            ]
        )
        with pytest.raises(ProviderAnswerError) as error:
            await run_one(transport)
        assert error.value.code == "answer_validation_failed"
        assert error.value.usage is not None
        assert len(transport.requests) == 1


async def test_provider_rejects_invalid_output_without_repair_request():
    cases = [
        ("malformed", "{not json"),
        ("oversized", "x" * 16385),
        ("unknown-reference", answer_content(citation_ids=["0" * 16])),
        ("extra-field", answer_content(extra="no")),
        ("wrong-type", answer_content(citation_ids="not-a-list")),
    ]
    for label, content in cases:
        transport = Transport([chat_response(content)])
        with pytest.raises(ProviderAnswerError) as error:
            await run_one(transport)
        assert error.value.code == "answer_validation_failed", label
        assert error.value.usage == ProviderUsage(
            prompt_tokens=9,
            completion_tokens=4,
            request_id="req_1",
            complete=True,
        ), label
        assert len(transport.requests) == 1, label


async def test_usage_validation_is_separate_from_answer_validation():
    # A valid answer with unusable usage is still an answer; settlement is the
    # caller's decision and no second request is made.
    transport = Transport([chat_response(answer_content(), usage=False)])
    reply = await run_one(transport)
    assert isinstance(reply.answer, AnswerDraft)
    assert reply.usage is None
    assert len(transport.requests) == 1


async def test_prepared_body_is_bounded_and_immutable_at_invoke():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(Transport([]))
    ) as client:
        provider = ChatProvider("secret-key", client)
        prepared = provider.prepare(messages(), [SEARCH_TOOL], manifest())
        assert len(prepared.body) <= MAX_BODY_BYTES
        assert prepared.body.startswith(b"{")
        tampered = type(prepared)(
            messages=prepared.messages,
            tools=prepared.tools,
            manifest=prepared.manifest,
            body=prepared.body + b" ",
            request_hash=prepared.request_hash,
            parameter_version=prepared.parameter_version,
        )
        with pytest.raises(AppError) as error:
            await provider.invoke(tampered, deadline=deadline())
    assert error.value.code == "internal_error"
