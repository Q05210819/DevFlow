"""Atomic chat reservations, the unknown-result ledger and bounded retention.

Real PostgreSQL and real Job leases; deterministic fake prices and usage. This
proves the ledger contract, never a provider bill or real billing behavior.
"""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import IntegrityError
from support.chat_env import chat_env as chat_env  # noqa: PLC0414
from support.job_env import job_env as job_env  # noqa: PLC0414
from support.knowledge_env import knowledge_env as knowledge_env  # noqa: PLC0414
from support.knowledge_env import repo_env as repo_env  # noqa: PLC0414

from devflow.audit.models import AuditLog
from devflow.chat import budget as budget_module
from devflow.chat.budget import BudgetService
from devflow.chat.models import AgentRun, BudgetLedger, RequestReceipt
from devflow.chat.schemas import ProviderUsage, Reservation
from devflow.errors import AppError
from devflow.jobs.models import Job
from devflow.jobs.queue import Queue
from devflow.knowledge.schemas import IndexSelection, QuerySelection

pytestmark = pytest.mark.asyncio

SHA = "a" * 40
USD = "USD"
MINOR = Decimal("0.000001")
PRICING = {
    "chat_cloud_enabled": True,
    "chat_budget_currency": USD,
    "chat_run_budget": Decimal("1.000000"),
    "chat_month_budget": Decimal("2.000000"),
    "chat_budget_minor_unit": MINOR,
    "chat_pricing_version": "test-pricing-v1",
    "chat_pricing_valid_until": datetime.now(UTC) + timedelta(days=1),
    "chat_input_price_per_million": Decimal("0.500000"),
    "chat_output_price_per_million": Decimal("1.500000"),
}
DEFAULT_AMOUNT = Decimal("0.501551")


def selection(*repository_ids):
    ids = tuple(repository_ids)
    return QuerySelection(
        selected_repository_ids=ids,
        indexes=(IndexSelection(repository_id=ids[0], sha=SHA, target="target"),),
    )


def service(env, **overrides):
    """A BudgetService reading an explicit deterministic fake price sheet."""
    return BudgetService(
        env.factory, env.settings.model_copy(update={**PRICING, **overrides})
    )


def usage(prompt=1000, completion=500, request_id="request-1", complete=True):
    return ProviderUsage(
        prompt_tokens=prompt,
        completion_tokens=completion,
        request_id=request_id,
        complete=complete,
    )


async def create_run(env, label):
    view = await env.chat.create(env.actor, (env.repository.id,), f"{label}-create")
    return await env.chat.submit(
        env.actor,
        view.id,
        selection(env.repository.id),
        f"question {label}",
        f"{label}-run",
    )


async def claim(env):
    lease = await Queue(env.factory, lease_seconds=300).claim(
        uuid4(), allowed_kinds=frozenset({"chat.run"})
    )
    assert lease is not None
    return lease


async def expire(env, lease):
    async with env.factory.begin() as db:
        await db.execute(
            update(Job)
            .where(Job.id == lease.job_id)
            .values(lease_expires_at=text("clock_timestamp() - interval '1 second'"))
        )


async def recover_and_reclaim(env, lease):
    await expire(env, lease)
    queue = Queue(
        env.factory,
        lease_seconds=300,
        retry_first_seconds=0,
        retry_second_seconds=0,
    )
    assert await queue.recover_expired() == 1
    return await claim(env)


async def ledger_totals(env, run_id):
    async with env.factory() as db:
        run = await db.scalar(select(BudgetLedger).where(BudgetLedger.run_id == run_id))
        month = await db.scalar(
            select(BudgetLedger).where(
                BudgetLedger.ledger_scope == "month",
                BudgetLedger.actor_id == env.actor.id,
            )
        )
    assert run is not None and month is not None
    return run.reserved, run.settled, month.reserved, month.settled


async def receipt(env, receipt_id):
    async with env.factory() as db:
        return await db.get(RequestReceipt, receipt_id)


async def reconciliation(env, run_id):
    async with env.factory() as db:
        return list(
            await db.scalars(
                select(AuditLog).where(
                    AuditLog.action == "chat.budget.reconciliation",
                    AuditLog.resource_id == run_id,
                )
            )
        )


async def mark_terminal(env, run_id):
    async with env.factory.begin() as db:
        now = await db.scalar(select(func.clock_timestamp()))
        run = await db.get(AgentRun, run_id)
        run.status = "failed"
        run.terminal_reason = "validation_error"
        run.terminal_at = now


async def test_reserve_full_caps_atomically(chat_env):
    env = chat_env
    first = await create_run(env, "caps")
    lease = await claim(env)
    priced = service(
        env,
        chat_input_price_per_million=Decimal("0.333333"),
        chat_output_price_per_million=Decimal("1.000001"),
    )

    reservation = await priced.reserve(
        lease, first.run_id, "model:0", "a" * 64, "params-v1"
    )
    assert reservation.input_cap == 1_000_000
    assert reservation.output_cap == 1_034
    # (0.333333*1_000_000 + 1.000001*1_034)/1_000_000 = 0.334367001034,
    # rounded up to the smallest configured currency unit.
    assert reservation.amount == Decimal("0.334368")
    assert reservation.currency == USD
    assert reservation.pricing_version == "test-pricing-v1"

    replay = await priced.reserve(lease, first.run_id, "model:0", "a" * 64, "params-v1")
    assert replay == reservation
    stored = await receipt(env, reservation.receipt_id)
    assert stored.status == "reserved"
    assert stored.request_hash == "a" * 64
    assert stored.parameter_version == "params-v1"
    assert stored.lease_owner == lease.owner
    assert stored.lease_generation == lease.generation
    assert stored.lease_attempt == lease.attempt
    assert stored.input_price_per_million == Decimal("0.333333")
    assert stored.output_price_per_million == Decimal("1.000001")
    assert stored.minor_unit == MINOR
    async with env.factory() as db:
        assert (await db.get(AgentRun, first.run_id)).model_requests_used == 1
    assert await ledger_totals(env, first.run_id) == (
        Decimal("0.334368"),
        Decimal(0),
        Decimal("0.334368"),
        Decimal(0),
    )

    await priced.mark_dispatched(lease, reservation.receipt_id)
    await priced.mark_dispatched(lease, reservation.receipt_id)
    assert (await receipt(env, reservation.receipt_id)).status == "dispatched"
    with pytest.raises(AppError) as error:
        await priced.reserve(lease, first.run_id, "model:0", "b" * 64, "params-v1")
    assert error.value.code == "idempotency_conflict"

    second = await priced.reserve(lease, first.run_id, "model:1", "c" * 64, "params-v1")
    assert second.receipt_id != reservation.receipt_id
    assert await ledger_totals(env, first.run_id) == (
        Decimal("0.668736"),
        Decimal(0),
        Decimal("0.668736"),
        Decimal(0),
    )
    with pytest.raises(AppError) as error:
        await priced.reserve(lease, first.run_id, "model:2", "d" * 64, "params-v1")
    assert error.value.code == "budget_exhausted"
    async with env.factory() as db:
        assert (await db.get(AgentRun, first.run_id)).model_requests_used == 2

    # A run bucket below one full reservation stops the very first call.
    third = await create_run(env, "run-bucket")
    third_lease = await claim(env)
    tight = service(
        env,
        chat_run_budget=Decimal("0.300000"),
        chat_input_price_per_million=Decimal("0.333333"),
        chat_output_price_per_million=Decimal("1.000001"),
    )
    with pytest.raises(AppError) as error:
        await tight.reserve(third_lease, third.run_id, "model:0", "e" * 64, "params-v1")
    assert error.value.code == "budget_exhausted"
    async with env.factory() as db:
        assert (await db.get(AgentRun, third.run_id)).model_requests_used == 0
        assert (
            await db.scalar(
                select(func.count())
                .select_from(RequestReceipt)
                .where(RequestReceipt.run_id == third.run_id)
            )
        ) == 0

    # The global admission lock lets exactly one of two concurrent reservations
    # through a month bucket narrower than twice the per-call amount.
    fourth = await create_run(env, "month-a")
    fifth = await create_run(env, "month-b")
    fourth_lease = await claim(env)
    fifth_lease = await claim(env)
    narrowed = service(
        env,
        chat_month_budget=Decimal("1.100000"),
        chat_input_price_per_million=Decimal("0.333333"),
        chat_output_price_per_million=Decimal("1.000001"),
    )
    outcomes = await asyncio.gather(
        narrowed.reserve(fourth_lease, fourth.run_id, "model:0", "f" * 64, "v1"),
        narrowed.reserve(fifth_lease, fifth.run_id, "model:0", "0" * 64, "v1"),
        return_exceptions=True,
    )
    successes = [item for item in outcomes if isinstance(item, Reservation)]
    failures = [item for item in outcomes if isinstance(item, AppError)]
    assert len(successes) == 1 and len(failures) == 1
    assert failures[0].code == "budget_exhausted"
    winner = fourth.run_id if isinstance(outcomes[0], Reservation) else fifth.run_id
    assert await ledger_totals(env, winner) == (
        Decimal("0.334368"),
        Decimal(0),
        Decimal("1.003104"),
        Decimal(0),
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"chat_cloud_enabled": False},
        {"chat_budget_currency": None},
        {"chat_budget_currency": "usd"},
        {"chat_run_budget": Decimal(0)},
        {"chat_month_budget": Decimal(-1)},
        {"chat_budget_minor_unit": Decimal(0)},
        {"chat_budget_minor_unit": Decimal("1.5")},
        {"chat_pricing_version": None},
        {"chat_pricing_version": "has space"},
        {"chat_pricing_valid_until": datetime(2020, 1, 1, tzinfo=UTC)},
        # Naive on purpose: the rejected case is the missing timezone.
        {"chat_pricing_valid_until": datetime(2030, 1, 1)},  # noqa: DTZ001
        {"chat_input_price_per_million": Decimal(0)},
        {"chat_output_price_per_million": Decimal("-0.01")},
    ],
)
async def test_missing_invalid_or_expired_price_sheet_is_rejected(chat_env, overrides):
    env = chat_env
    run = await create_run(env, f"config-{uuid4().hex[:8]}")
    lease = await claim(env)
    with pytest.raises(AppError) as error:
        await service(env, **overrides).reserve(
            lease, run.run_id, "model:0", "a" * 64, "params-v1"
        )
    assert error.value.code == "model_not_configured"
    async with env.factory() as db:
        assert (await db.get(AgentRun, run.run_id)).model_requests_used == 0


async def test_settings_reject_incomplete_or_invalid_cloud_budget(settings_factory):
    assert settings_factory().chat_cloud_enabled is False
    for overrides in (
        {"chat_cloud_enabled": False},
        {"chat_budget_currency": None},
        {"chat_budget_currency": "usd"},
        {"chat_run_budget": Decimal(0)},
        {"chat_month_budget": Decimal(-1)},
        {"chat_budget_minor_unit": Decimal(0)},
        {"chat_budget_minor_unit": Decimal("1.5")},
        {"chat_pricing_version": None},
        {"chat_pricing_version": "has space"},
        # Naive on purpose: the rejected case is the missing timezone.
        {"chat_pricing_valid_until": datetime(2030, 1, 1)},  # noqa: DTZ001
        {"chat_input_price_per_million": Decimal(0)},
        {"chat_output_price_per_million": Decimal("-0.01")},
    ):
        with pytest.raises(ValidationError):
            settings_factory(**{**PRICING, **overrides})
    for field in PRICING:
        with pytest.raises(ValidationError):
            settings_factory(**{**PRICING, field: None})


async def test_unknown_keeps_reservation_after_cancel_delete_replay(chat_env):
    env = chat_env
    priced = service(env)

    # A dispatched call whose worker dies before usage is never resent or
    # released, even when the next attempt holds a legal fresh lease.
    first = await create_run(env, "unknown-dispatched")
    lease = await claim(env)
    reservation = await priced.reserve(
        lease, first.run_id, "model:0", "a" * 64, "params-v1"
    )
    await priced.mark_dispatched(lease, reservation.receipt_id)
    before = await ledger_totals(env, first.run_id)
    fresh = await recover_and_reclaim(env, lease)
    for key, digest in (("model:0", "a" * 64), ("model:1", "b" * 64)):
        with pytest.raises(AppError) as error:
            await priced.reserve(fresh, first.run_id, key, digest, "params-v1")
        assert error.value.code == "model_call_outcome_unknown"
    with pytest.raises(AppError) as error:
        await priced.mark_dispatched(fresh, reservation.receipt_id)
    assert error.value.code == "model_call_outcome_unknown"
    with pytest.raises(AppError) as error:
        await priced.settle(fresh, reservation.receipt_id, usage())
    assert error.value.code == "model_call_outcome_unknown"
    assert (await receipt(env, reservation.receipt_id)).status == "unknown"
    async with env.factory() as db:
        assert (await db.get(AgentRun, first.run_id)).model_requests_used == 1
    assert await ledger_totals(env, first.run_id) == before
    assert [row.reason_code for row in await reconciliation(env, first.run_id)] == [
        "model_call_outcome_unknown"
    ]

    # Fee identity survives deletion: the run cannot erase its receipt or ledger.
    with pytest.raises(IntegrityError):
        async with env.factory.begin() as db:
            run = await db.get(AgentRun, first.run_id)
            await db.delete(run)
    assert (await receipt(env, reservation.receipt_id)).status == "unknown"
    assert await ledger_totals(env, first.run_id) == before

    # A crash after the receipt was persisted but before the dispatched marker
    # is conservatively unknown: absence of a marker never proves no send.
    second = await create_run(env, "unknown-reserved")
    lease = await claim(env)
    reserved = await priced.reserve(
        lease, second.run_id, "model:0", "c" * 64, "params-v1"
    )
    before = await ledger_totals(env, second.run_id)
    fresh = await recover_and_reclaim(env, lease)
    with pytest.raises(AppError) as error:
        await priced.reserve(fresh, second.run_id, "model:0", "c" * 64, "params-v1")
    assert error.value.code == "model_call_outcome_unknown"
    assert (await receipt(env, reserved.receipt_id)).status == "unknown"
    assert await ledger_totals(env, second.run_id) == before

    # Cancellation revokes the old lease first; stale actions cannot mutate the
    # receipt, the run counter or either bucket afterwards.
    third = await create_run(env, "unknown-cancel")
    lease = await claim(env)
    reservation = await priced.reserve(
        lease, third.run_id, "model:0", "d" * 64, "params-v1"
    )
    await priced.mark_dispatched(lease, reservation.receipt_id)
    before = await ledger_totals(env, third.run_id)
    assert await Queue(env.factory).fail(lease, "attempt_timeout", False)
    for action in (
        lambda: priced.reserve(lease, third.run_id, "model:0", "d" * 64, "v1"),
        lambda: priced.mark_dispatched(lease, reservation.receipt_id),
        lambda: priced.mark_unknown(lease, reservation.receipt_id),
        lambda: priced.settle(lease, reservation.receipt_id, usage()),
    ):
        with pytest.raises(AppError) as error:
            await action()
        assert error.value.code == "lease_expired"
    assert (await receipt(env, reservation.receipt_id)).status == "dispatched"
    assert await ledger_totals(env, third.run_id) == before


async def test_usage_does_not_double_count_reasoning(chat_env):
    env = chat_env
    priced = service(env)
    run = await create_run(env, "usage")
    lease = await claim(env)
    reservation = await priced.reserve(
        lease, run.run_id, "model:0", "a" * 64, "params-v1"
    )
    await priced.mark_dispatched(lease, reservation.receipt_id)

    consumed = usage(prompt=1000, completion=500, request_id="provider-1")
    await priced.settle(lease, reservation.receipt_id, consumed)
    # completion_tokens already contains reasoning tokens: 0.0005 + 0.00075.
    assert await ledger_totals(env, run.run_id) == (
        Decimal(0),
        Decimal("0.001250"),
        Decimal(0),
        Decimal("0.001250"),
    )
    stored = await receipt(env, reservation.receipt_id)
    assert stored.status == "settled"
    assert stored.provider_request_id == "provider-1"
    assert stored.usage == {
        "prompt_tokens": 1000,
        "completion_tokens": 500,
        "request_id": "provider-1",
        "complete": True,
    }
    assert len(json.dumps(stored.usage).encode()) <= 4096
    assert stored.dispatched_at is not None and stored.settled_at is not None

    await priced.settle(lease, reservation.receipt_id, consumed)
    with pytest.raises(AppError) as error:
        await priced.settle(
            lease,
            reservation.receipt_id,
            usage(prompt=1000, completion=900, request_id="provider-1"),
        )
    assert error.value.code == "idempotency_conflict"
    assert await ledger_totals(env, run.run_id) == (
        Decimal(0),
        Decimal("0.001250"),
        Decimal(0),
        Decimal("0.001250"),
    )


async def test_month_boundary_does_not_move_old_reservation(chat_env):
    env = chat_env
    priced = service(env)
    run = await create_run(env, "month-boundary")
    lease = await claim(env)
    reservation = await priced.reserve(
        lease, run.run_id, "model:0", "a" * 64, "params-v1"
    )
    await priced.mark_dispatched(lease, reservation.receipt_id)

    current = datetime.now(UTC).date().replace(day=1)
    old = (current - timedelta(days=1)).replace(day=1)
    async with env.factory.begin() as db:
        await db.execute(
            update(RequestReceipt)
            .where(RequestReceipt.id == reservation.receipt_id)
            .values(month_start=old)
        )
        await db.execute(
            update(BudgetLedger)
            .where(
                BudgetLedger.ledger_scope == "month",
                BudgetLedger.actor_id == env.actor.id,
            )
            .values(month_start=old)
        )
        db.add(
            BudgetLedger(
                ledger_scope="month",
                actor_id=env.actor.id,
                month_start=current,
                currency=USD,
            )
        )

    await priced.settle(lease, reservation.receipt_id, usage())
    async with env.factory() as db:
        old_ledger = await db.scalar(
            select(BudgetLedger).where(
                BudgetLedger.ledger_scope == "month",
                BudgetLedger.actor_id == env.actor.id,
                BudgetLedger.month_start == old,
            )
        )
        new_ledger = await db.scalar(
            select(BudgetLedger).where(
                BudgetLedger.ledger_scope == "month",
                BudgetLedger.actor_id == env.actor.id,
                BudgetLedger.month_start == current,
            )
        )
        run_ledger = await db.scalar(
            select(BudgetLedger).where(BudgetLedger.run_id == run.run_id)
        )
    assert (old_ledger.reserved, old_ledger.settled) == (
        Decimal(0),
        Decimal("0.001250"),
    )
    assert (new_ledger.reserved, new_ledger.settled) == (
        Decimal(0),
        Decimal(0),
    )
    assert (run_ledger.reserved, run_ledger.settled) == (
        Decimal(0),
        Decimal("0.001250"),
    )


async def test_over_cap_usage_stops_further_calls_and_records_anomaly(chat_env):
    env = chat_env
    priced = service(env)
    run = await create_run(env, "over-cap")
    lease = await claim(env)
    reservation = await priced.reserve(
        lease, run.run_id, "model:0", "a" * 64, "params-v1"
    )
    await priced.mark_dispatched(lease, reservation.receipt_id)
    before = await ledger_totals(env, run.run_id)
    with pytest.raises(AppError) as error:
        await priced.settle(
            lease,
            reservation.receipt_id,
            usage(prompt=1_000_001, completion=0, request_id="over"),
        )
    assert error.value.code == "model_call_outcome_unknown"
    assert (await receipt(env, reservation.receipt_id)).status == "unknown"
    assert await ledger_totals(env, run.run_id) == before
    audit = await reconciliation(env, run.run_id)
    assert len(audit) == 1
    assert audit[0].reason_code == "model_call_outcome_unknown"
    assert audit[0].resource_type == "chat_run"

    with pytest.raises(AppError) as error:
        await priced.reserve(lease, run.run_id, "model:1", "b" * 64, "params-v1")
    assert error.value.code == "model_call_outcome_unknown"

    # An incomplete usage report is equally untrusted; the reservation stays.
    second = await create_run(env, "incomplete")
    second_lease = await claim(env)
    second_reservation = await priced.reserve(
        second_lease, second.run_id, "model:0", "c" * 64, "params-v1"
    )
    await priced.mark_dispatched(second_lease, second_reservation.receipt_id)
    before = await ledger_totals(env, second.run_id)
    with pytest.raises(AppError) as error:
        await priced.settle(
            second_lease,
            second_reservation.receipt_id,
            usage(complete=False),
        )
    assert error.value.code == "model_call_outcome_unknown"
    assert await ledger_totals(env, second.run_id) == before

    # An existing bucket above a newly lowered budget is a reconciliation
    # anomaly and stops subsequent calls without inventing a second receipt.
    third = await create_run(env, "bucket-anomaly")
    third_lease = await claim(env)
    await priced.reserve(third_lease, third.run_id, "model:0", "d" * 64, "params-v1")
    lower = service(env, chat_run_budget=Decimal("0.100000"))
    with pytest.raises(AppError) as error:
        await lower.reserve(third_lease, third.run_id, "model:1", "e" * 64, "v1")
    assert error.value.code == "budget_exhausted"
    assert [row.reason_code for row in await reconciliation(env, third.run_id)] == [
        "budget_exhausted"
    ]
    async with env.factory() as db:
        assert (await db.get(AgentRun, third.run_id)).model_requests_used == 1
        assert (
            await db.scalar(
                select(func.count())
                .select_from(RequestReceipt)
                .where(RequestReceipt.run_id == third.run_id)
            )
        ) == 1


async def test_receipt_retention_only_prunes_settled_terminal(chat_env):
    env = chat_env
    priced = service(env)

    async def settled_run(label):
        run = await create_run(env, label)
        lease = await claim(env)
        reservation = await priced.reserve(
            lease, run.run_id, "model:0", "a" * 64, "params-v1"
        )
        await priced.mark_dispatched(lease, reservation.receipt_id)
        await priced.settle(
            lease, reservation.receipt_id, usage(request_id=f"req-{label}")
        )
        return run, reservation

    old, old_receipt = await settled_run("old")
    fresh, fresh_receipt = await settled_run("fresh")
    _, active_receipt = await settled_run("active")
    await mark_terminal(env, old.run_id)
    await mark_terminal(env, fresh.run_id)
    unknown_run = await create_run(env, "unknown-retained")
    lease = await claim(env)
    unknown = await priced.reserve(
        lease, unknown_run.run_id, "model:0", "b" * 64, "params-v1"
    )
    await priced.mark_dispatched(lease, unknown.receipt_id)
    await priced.mark_unknown(lease, unknown.receipt_id)
    await mark_terminal(env, unknown_run.run_id)

    async with env.factory.begin() as db:
        stale = await db.scalar(
            select(func.clock_timestamp() - text("interval '91 days'"))
        )
        await db.execute(
            update(RequestReceipt)
            .where(
                RequestReceipt.id.in_(
                    (old_receipt.receipt_id, active_receipt.receipt_id)
                )
            )
            .values(settled_at=stale)
        )
        await db.execute(
            update(RequestReceipt)
            .where(RequestReceipt.id == unknown.receipt_id)
            .values(created_at=stale)
        )

    assert await priced.prune_receipts(limit=100) == 1
    async with env.factory() as db:
        remaining = set(
            await db.scalars(
                select(RequestReceipt.id).where(
                    RequestReceipt.id.in_(
                        (
                            old_receipt.receipt_id,
                            fresh_receipt.receipt_id,
                            active_receipt.receipt_id,
                            unknown.receipt_id,
                        )
                    )
                )
            )
        )
        months = await db.scalar(
            select(func.count())
            .select_from(BudgetLedger)
            .where(BudgetLedger.ledger_scope == "month")
        )
    assert remaining == {
        fresh_receipt.receipt_id,
        active_receipt.receipt_id,
        unknown.receipt_id,
    }
    assert months == 1


async def test_receipt_accounting_boundaries_are_fixed(chat_env, monkeypatch):
    env = chat_env
    assert budget_module.ACCOUNTED_RECEIPT_BYTES == 4 * 1024
    assert budget_module.MAX_RECEIPT_BYTES == 32 * 1024 * 1024
    assert budget_module.MAX_RECEIPTS == 8192

    with pytest.raises(ValidationError):
        usage(prompt=-1)
    with pytest.raises(ValidationError):
        usage(request_id="")
    with pytest.raises(ValidationError):
        usage(request_id="x" * 129)

    priced = service(env)
    first = await create_run(env, "cap-first")
    lease = await claim(env)
    await priced.reserve(lease, first.run_id, "model:0", "a" * 64, "params-v1")
    monkeypatch.setattr(budget_module, "MAX_RECEIPTS", 1)
    second = await create_run(env, "cap-second")
    second_lease = await claim(env)
    with pytest.raises(AppError) as error:
        await priced.reserve(
            second_lease, second.run_id, "model:0", "b" * 64, "params-v1"
        )
    assert error.value.code == "storage_exhausted"
