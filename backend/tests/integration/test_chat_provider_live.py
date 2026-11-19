"""Gated real DashScope protocol check; disabled unless explicitly enabled.

This test spends real money and calls a real provider, so it refuses to run
without a rotated local key file, a complete positive price sheet and an explicit
``DEVFLOW_CHAT_LIVE=1`` opt-in. It sends one synthetic, evidence-free question and
never reads a repository, a conversation or a stored manifest. A pass only shows
one accepted round trip with this account; it does not prove provider-wide
compliance, token accuracy or an absolute final bill.
"""

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from langchain_core.messages import HumanMessage, SystemMessage

from devflow.chat.context import SYSTEM_PROMPT
from devflow.chat.provider import ChatProvider
from devflow.knowledge.schemas import EvidenceManifest
from devflow.security import read_secret_file

pytestmark = [pytest.mark.asyncio, pytest.mark.live_chat]

ENABLE = "DEVFLOW_CHAT_LIVE"
KEY_FILE = "DEVFLOW_CHAT_LIVE_KEY_FILE"


def enabled() -> bool:
    return os.environ.get(ENABLE) == "1" and bool(os.environ.get(KEY_FILE))


def live_settings():
    """Reuse the production price-sheet validation for the live call."""

    from devflow.config import Settings

    return Settings(
        _env_file=None,
        app_env="test",
        public_base_url="http://127.0.0.1:8000",
        chat_cloud_enabled=True,
    )


@pytest.mark.skipif(
    not enabled(),
    reason="Set DEVFLOW_CHAT_LIVE=1 and DEVFLOW_CHAT_LIVE_KEY_FILE to spend real budget",
)
async def test_live_chat_protocol_roundtrip():
    settings = live_settings()
    assert settings.chat_run_budget is not None
    assert settings.chat_month_budget is not None
    key = read_secret_file(Path(os.environ[KEY_FILE])).decode("utf-8")
    assert key and len(key) >= 16

    async with httpx.AsyncClient() as client:
        provider = ChatProvider(key, client)
        prepared = provider.prepare(
            [
                SystemMessage(SYSTEM_PROMPT),
                HumanMessage(
                    "This request intentionally carries no repository evidence. "
                    "Answer that the evidence is insufficient."
                ),
            ],
            [],
            EvidenceManifest(entries=()),
        )
        reply = await provider.invoke(
            prepared, deadline=datetime.now(UTC) + timedelta(seconds=90)
        )

    assert reply.usage is not None
    assert reply.usage.complete is True
    assert reply.usage.request_id
    assert reply.usage.prompt_tokens > 0
    assert reply.usage.completion_tokens > 0
    assert reply.answer is not None
    assert reply.tool_query is None
    assert reply.answer.insufficient_evidence is True
    assert reply.answer.citation_ids == ()
    # The body the adapter actually sent is the one the ledger hashed.
    assert len(prepared.body) <= 24 * 1024
    assert prepared.parameter_version == "qwen3.8-max-chat-v1"
