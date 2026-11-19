"""Cloud provider wiring: explicit opt-in, never a hidden default."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from devflow.chat.provider import ChatProvider
from devflow.worker import chat_provider

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


@pytest.mark.asyncio
async def test_default_deployment_has_no_cloud_provider(settings_factory):
    async with chat_provider(settings_factory()) as provider:
        assert provider is None


@pytest.mark.asyncio
async def test_missing_or_blank_key_keeps_cloud_disabled(settings_factory, tmp_path):
    missing = settings_factory(**PRICING, chat_api_key_file=tmp_path / "chat_api_key")
    async with chat_provider(missing) as provider:
        assert provider is None

    blank = tmp_path / "blank_api_key"
    blank.write_text("   \n", encoding="utf-8")
    empty = settings_factory(**PRICING, chat_api_key_file=blank)
    async with chat_provider(empty) as provider:
        assert provider is None


@pytest.mark.asyncio
async def test_enabled_cloud_with_a_readable_key_builds_the_fixed_provider(
    settings_factory, tmp_path
):
    key = tmp_path / "chat_api_key"
    key.write_text("test-key-0123456789", encoding="utf-8")
    settings = settings_factory(**PRICING, chat_api_key_file=key)
    async with chat_provider(settings) as provider:
        assert isinstance(provider, ChatProvider)
