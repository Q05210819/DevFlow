"""The fixed DashScope Chat Completions adapter for private chat.

Only evidence-bearing messages reach this module. It builds the complete JSON
body, hashes those exact bytes and, at invoke time, re-checks and sends them
unchanged. That single serialization exit is the authority for the 24 KiB
limit, the request hash stored in the budget receipt, and the final citation
manifest. The OpenAI-compatible endpoint is fixed; the Responses API and SDK
retries are never used.
"""

import hashlib
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx
from langchain_core.messages import BaseMessage, convert_to_openai_messages
from pydantic import ValidationError

from devflow.chat.schemas import AnswerDraft, ProviderUsage
from devflow.errors import AppError
from devflow.knowledge.schemas import EvidenceManifest

MODEL = "qwen3.8-max"
BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
CHAT_COMPLETIONS_URL = f"{BASE_URL}/chat/completions"
PARAMETER_VERSION = "qwen3.8-max-chat-v1"
MAX_BODY_BYTES = 24 * 1024
MAX_ANSWER_BYTES = 16 * 1024
MAX_RESPONSE_BYTES = 64 * 1024
MAX_COMPLETION_TOKENS = 1024
TOOL_NAME = "search_repository_evidence"
TOOL_QUERY_BYTES = 2048
_REQUEST_ID = re.compile(r"[!-~]{1,128}")
_TOOL_CALL_ID = re.compile(r"[!-~]{1,128}")


@dataclass(frozen=True, slots=True)
class PreparedCall:
    """One immutable request: the body bytes are the only serialization."""

    messages: tuple[BaseMessage, ...]
    tools: tuple[dict[str, Any], ...]
    manifest: EvidenceManifest
    body: bytes
    request_hash: str
    parameter_version: str


@dataclass(frozen=True, slots=True)
class ProviderReply:
    """Either a validated answer or one tool request, never both."""

    answer: AnswerDraft | None
    tool_query: str | None
    tool_call_id: str | None
    usage: ProviderUsage | None

    @property
    def request_id(self) -> str | None:
        return self.usage.request_id if self.usage is not None else None


class ProviderAnswerError(AppError):
    """Untrusted output was rejected; trusted usage remains settleable."""

    def __init__(self, usage: ProviderUsage | None) -> None:
        super().__init__(
            "answer_validation_failed", 422, "Model answer failed validation"
        )
        self.usage = usage


def _body_too_large() -> AppError:
    return AppError("context_limit", 422, "Chat request body exceeds 24 KiB")


def _internal(message: str) -> AppError:
    return AppError("internal_error", 500, message)


def _unavailable() -> AppError:
    return AppError("service_unavailable", 503, "Model provider unavailable")


def _outcome_unknown() -> AppError:
    return AppError("model_call_outcome_unknown", 409, "Model call outcome unknown")


def _deadline_exceeded() -> AppError:
    return AppError("attempt_timeout", 503, "Model request deadline exceeded")


class ChatProvider:
    """A single fixed chat-completions request with no automatic retry."""

    def __init__(self, api_key: str, client: httpx.AsyncClient) -> None:
        if not api_key or not api_key.strip():
            raise AppError("model_not_configured", 503, "Cloud chat is not configured")
        self._api_key = api_key
        self._client = client

    def prepare(
        self,
        messages: Sequence[BaseMessage],
        tools: Sequence[dict[str, Any]],
        manifest: EvidenceManifest,
    ) -> PreparedCall:
        """Serialize the complete body once and hash exactly these bytes."""
        if not messages or not all(
            isinstance(message, BaseMessage) for message in messages
        ):
            raise _internal("Chat messages must be LangChain messages")
        if not all(isinstance(tool, dict) for tool in tools):
            raise _internal("Chat tools must be JSON objects")
        payload = {
            "model": MODEL,
            "stream": False,
            "messages": _wire_messages(messages),
            "max_completion_tokens": MAX_COMPLETION_TOKENS,
            "enable_thinking": False,
            "tools": list(tools),
            "tool_choice": "auto",
        }
        try:
            body = json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError):
            raise _internal("Chat request is not serializable") from None
        if len(body) > MAX_BODY_BYTES:
            raise _body_too_large()
        return PreparedCall(
            messages=tuple(messages),
            tools=tuple(tools),
            manifest=manifest,
            body=body,
            request_hash=hashlib.sha256(body).hexdigest(),
            parameter_version=PARAMETER_VERSION,
        )

    async def invoke(
        self, prepared: PreparedCall, *, deadline: datetime
    ) -> ProviderReply:
        """Send the prepared bytes once; never repair or resend them."""
        if (
            prepared.parameter_version != PARAMETER_VERSION
            or not isinstance(prepared.body, bytes)
            or len(prepared.body) > MAX_BODY_BYTES
            or hashlib.sha256(prepared.body).hexdigest() != prepared.request_hash
        ):
            raise _internal("Prepared chat body changed before dispatch")
        remaining = (deadline - datetime.now(UTC)).total_seconds()
        if remaining <= 0:
            raise _deadline_exceeded()
        timeout = httpx.Timeout(remaining, connect=min(remaining, 10.0))
        try:
            response = await self._client.post(
                CHAT_COMPLETIONS_URL,
                content=prepared.body,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                timeout=timeout,
            )
        except (httpx.ConnectError, httpx.ConnectTimeout) as error:
            raise _unavailable() from error
        except httpx.TransportError as error:
            # The request may have reached the provider; a replay could bill a
            # second completion, so this is never retryable.
            raise _outcome_unknown() from error
        if response.status_code != 200:
            raise _provider_status(response.status_code)
        if len(response.content) > MAX_RESPONSE_BYTES:
            raise ProviderAnswerError(None)
        try:
            payload = response.json()
        except ValueError:
            raise ProviderAnswerError(None) from None
        if not isinstance(payload, dict):
            raise ProviderAnswerError(None)
        request_id = _request_id(payload, response)
        usage = _usage(payload, request_id)
        return _parse_reply(payload, prepared.manifest, usage)


def _wire_messages(messages: Sequence[BaseMessage]) -> list[dict[str, Any]]:
    wire = convert_to_openai_messages(list(messages))
    for message in wire:
        if (
            message.get("role") == "assistant"
            and message.get("tool_calls")
            and message.get("content") == ""
        ):
            message["content"] = None
    return wire


def _request_id(payload: dict[str, Any], response: httpx.Response) -> str | None:
    candidate = payload.get("id")
    if not isinstance(candidate, str) or not _REQUEST_ID.fullmatch(candidate):
        candidate = response.headers.get("x-request-id")
    if not isinstance(candidate, str) or not _REQUEST_ID.fullmatch(candidate):
        candidate = response.headers.get("x-dashscope-request-id")
    if not isinstance(candidate, str) or not _REQUEST_ID.fullmatch(candidate):
        return None
    return candidate


def _usage(payload: dict[str, Any], request_id: str | None) -> ProviderUsage | None:
    if request_id is None:
        return None
    raw = payload.get("usage")
    if not isinstance(raw, dict):
        return None
    prompt = raw.get("prompt_tokens")
    completion = raw.get("completion_tokens")
    if (
        type(prompt) is not int
        or type(completion) is not int
        or prompt < 0
        or completion < 0
    ):
        return None
    try:
        return ProviderUsage(
            prompt_tokens=prompt,
            completion_tokens=completion,
            request_id=request_id,
            complete=True,
        )
    except ValidationError:
        return None


def _parse_reply(
    payload: dict[str, Any],
    manifest: EvidenceManifest,
    usage: ProviderUsage | None,
) -> ProviderReply:
    try:
        choices = payload["choices"]
        if not isinstance(choices, list) or len(choices) != 1:
            raise ValueError("Expected exactly one choice")
        choice = choices[0]
        if not isinstance(choice, dict) or choice.get("index") != 0:
            raise ValueError("Unexpected choice index")
        message = choice["message"]
        if not isinstance(message, dict) or message.get("role") != "assistant":
            raise ValueError("Unexpected message role")
        if message.get("tool_calls") is not None:
            return _tool_reply(message, usage)
        return _answer_reply(message, manifest, usage)
    except ProviderAnswerError:
        raise
    except (KeyError, TypeError, ValueError):
        raise ProviderAnswerError(usage) from None


def _tool_reply(message: dict[str, Any], usage: ProviderUsage | None) -> ProviderReply:
    content = message.get("content")
    if content not in (None, ""):
        raise ProviderAnswerError(usage)
    calls = message["tool_calls"]
    if not isinstance(calls, list) or len(calls) != 1:
        raise ProviderAnswerError(usage)
    call = calls[0]
    if not isinstance(call, dict) or call.get("type") != "function":
        raise ProviderAnswerError(usage)
    call_id = call.get("id")
    function = call.get("function")
    if (
        not isinstance(call_id, str)
        or not _TOOL_CALL_ID.fullmatch(call_id)
        or not isinstance(function, dict)
        or function.get("name") != TOOL_NAME
    ):
        raise ProviderAnswerError(usage)
    arguments = function.get("arguments")
    if not isinstance(arguments, str):
        raise ProviderAnswerError(usage)
    try:
        parsed = json.loads(arguments)
    except ValueError:
        raise ProviderAnswerError(usage) from None
    if not isinstance(parsed, dict) or set(parsed) != {"query"}:
        raise ProviderAnswerError(usage)
    query = parsed["query"]
    if (
        not isinstance(query, str)
        or not query
        or len(query.encode("utf-8")) > TOOL_QUERY_BYTES
    ):
        raise ProviderAnswerError(usage)
    return ProviderReply(
        answer=None,
        tool_query=query,
        tool_call_id=call_id,
        usage=usage,
    )


def _answer_reply(
    message: dict[str, Any],
    manifest: EvidenceManifest,
    usage: ProviderUsage | None,
) -> ProviderReply:
    content = message.get("content")
    if not isinstance(content, str):
        raise ProviderAnswerError(usage)
    try:
        encoded = content.encode("utf-8")
    except UnicodeEncodeError:
        raise ProviderAnswerError(usage) from None
    if not encoded or len(encoded) > MAX_ANSWER_BYTES:
        raise ProviderAnswerError(usage)
    try:
        parsed = json.loads(content)
        answer = AnswerDraft.model_validate(parsed)
    except (ValueError, ValidationError):
        raise ProviderAnswerError(usage) from None
    known = {entry.citation_id for entry in manifest.entries}
    citations = answer.citation_ids
    if len(set(citations)) != len(citations) or not set(citations) <= known:
        raise ProviderAnswerError(usage)
    return ProviderReply(
        answer=answer,
        tool_query=None,
        tool_call_id=None,
        usage=usage,
    )


def _provider_status(status: int) -> AppError:
    if status in (401, 403):
        return AppError(
            "model_not_configured", 503, "Model provider rejected credentials"
        )
    if status == 429 or status >= 500:
        return _unavailable()
    return AppError("internal_error", 500, "Model provider rejected the request")
