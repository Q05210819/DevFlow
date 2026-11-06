"""Atomic reservations and the unknown-result ledger for private chat.

A model call reserves its full input context plus the maximum output deviation
against both a run bucket and the actor's UTC month bucket before any HTTP is
attempted. Only trusted provider usage releases the confirmed unused amount.
A dispatched call whose outcome is unknown keeps its reservation indefinitely:
nothing here can prove the provider did not bill it, so nothing here releases
it. No price, currency or provider identity is hardcoded.
"""

import re
from datetime import UTC, datetime, timedelta
from decimal import ROUND_CEILING, Decimal
from uuid import UUID

from sqlalchemy import delete, func, select, text
from sqlalchemy.dialects.postgresql import insert

from devflow.audit.service import AuditEntry, write_audit
from devflow.chat.models import AgentRun, BudgetLedger, RequestReceipt
from devflow.chat.schemas import ProviderUsage, Reservation
from devflow.errors import AppError
from devflow.jobs.models import Job
from devflow.jobs.queue import Lease
from devflow.sync.scope import transaction

_KIND = "chat.run"
_RECEIPT_KIND = "model"
_MAX_REQUESTS = 2
_INPUT_CAP = 1_000_000
_OUTPUT_CAP = 1_034
_MICRO = Decimal(1_000_000)

# One receipt accounts for at most 4 KiB of fee evidence; the global cap is
# therefore a fixed receipt count as well as a byte ceiling.
ACCOUNTED_RECEIPT_BYTES = 4 * 1024
MAX_RECEIPT_BYTES = 32 * 1024 * 1024
MAX_RECEIPTS = MAX_RECEIPT_BYTES // ACCOUNTED_RECEIPT_BYTES
RETENTION = timedelta(days=90)

_BUDGET_LOCK = "devflow:chat:budget"
_CALL_KEY = re.compile(r"model:(0|[1-9][0-9]{0,2})")
_HASH = re.compile(r"[0-9a-f]{64}")
_CURRENCY = re.compile(r"[A-Z]{3}")
_NUMERIC_LIMIT = Decimal(10) ** 18


def _not_configured() -> AppError:
    return AppError("model_not_configured", 503, "Cloud chat is not configured")


def _unknown() -> AppError:
    return AppError("model_call_outcome_unknown", 409, "Model call outcome is unknown")


def _conflict() -> AppError:
    return AppError(
        "idempotency_conflict", 409, "Logical call conflicts with its receipt"
    )


def _budget() -> AppError:
    return AppError("budget_exhausted", 409, "Chat budget is exhausted")


def _storage() -> AppError:
    return AppError("storage_exhausted", 409, "Chat receipt storage exhausted")


def _invalid() -> AppError:
    return AppError("validation_error", 422, "Invalid budget request")


def _lease_lost() -> AppError:
    return AppError("lease_expired", 409, "Model lease is no longer valid")


def _inconsistent() -> AppError:
    return AppError("internal_error", 500, "Chat budget state is inconsistent")


class _Rejected:
    """Commit the transition, then surface the fixed error to the caller."""

    __slots__ = ("error",)

    def __init__(self, error: AppError) -> None:
        self.error = error


def _positive_decimal(value: object) -> bool:
    return (
        isinstance(value, Decimal)
        and value.is_finite()
        and Decimal(0) < value < _NUMERIC_LIMIT
        and value.as_tuple().exponent >= -6
    )


class BudgetService:
    """Reserve, dispatch, settle and retain bounded chat request evidence."""

    def __init__(self, factory, settings):
        self.factory, self.settings = factory, settings

    async def reserve(
        self,
        lease: Lease,
        run_id: UUID,
        call_key: str,
        request_hash: str,
        parameter_version: str,
    ) -> Reservation:
        ordinal = self._ordinal(call_key)
        self._hash(request_hash)
        self._version(parameter_version)
        async with transaction(self.factory) as db:
            outcome = await self._reserve(
                db, lease, run_id, ordinal, request_hash, parameter_version
            )
        if isinstance(outcome, _Rejected):
            raise outcome.error
        return outcome

    async def mark_dispatched(self, lease: Lease, receipt_id: UUID) -> None:
        async with transaction(self.factory) as db:
            outcome = await self._dispatch(db, lease, receipt_id)
        if isinstance(outcome, _Rejected):
            raise outcome.error

    async def settle(
        self, lease: Lease, receipt_id: UUID, usage: ProviderUsage
    ) -> None:
        try:
            usage = ProviderUsage.model_validate(usage)
        except (TypeError, ValueError):
            raise _invalid() from None
        async with transaction(self.factory) as db:
            outcome = await self._settle(db, lease, receipt_id, usage)
        if isinstance(outcome, _Rejected):
            raise outcome.error

    async def mark_unknown(self, lease: Lease, receipt_id: UUID) -> None:
        async with transaction(self.factory) as db:
            outcome = await self._mark_unknown(db, lease, receipt_id)
        if isinstance(outcome, _Rejected):
            raise outcome.error

    async def prune_receipts(self, *, limit: int = 100) -> int:
        """Delete settled terminal evidence past retention; keep everything else."""
        if (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or not 1 <= limit <= 1000
        ):
            raise ValueError("limit must be between 1 and 1000")
        async with transaction(self.factory) as db:
            await self._admit(db)
            stale = (
                select(RequestReceipt.id)
                .join(AgentRun, AgentRun.id == RequestReceipt.run_id)
                .where(
                    RequestReceipt.status == "settled",
                    AgentRun.terminal_at.is_not(None),
                    RequestReceipt.settled_at
                    <= text("clock_timestamp() - interval '90 days'"),
                )
                .order_by(RequestReceipt.settled_at)
                .limit(limit)
                .with_for_update(of=RequestReceipt, skip_locked=True)
            )
            ids = list(await db.scalars(stale))
            if not ids:
                return 0
            await db.execute(delete(RequestReceipt).where(RequestReceipt.id.in_(ids)))
        return len(ids)

    async def _reserve(
        self, db, lease, run_id, ordinal, request_hash, parameter_version
    ):
        await self._admit(db)
        job, now = await self._fenced(db, lease)
        run = await db.scalar(
            select(AgentRun)
            .where(AgentRun.id == run_id, AgentRun.job_id == job.id)
            .with_for_update()
        )
        if (
            run is None
            or run.actor_id != job.requested_by
            or run.status not in ("queued", "running")
            or run.terminal_at is not None
        ):
            raise _lease_lost()
        existing = await db.scalar(
            select(RequestReceipt)
            .where(
                RequestReceipt.run_id == run.id,
                RequestReceipt.kind == _RECEIPT_KIND,
                RequestReceipt.ordinal == ordinal,
            )
            .with_for_update()
        )
        # Any unknown call poisons the rest of the run: the run cannot prove a
        # safe recovery and must never dispatch another request, including a
        # replay of an earlier reservation held by this same lease.
        poisoned = await db.scalar(
            select(RequestReceipt.id)
            .where(
                RequestReceipt.run_id == run.id,
                RequestReceipt.status == "unknown",
            )
            .limit(1)
        )
        if existing is not None:
            if poisoned is not None and existing.status != "unknown":
                if existing.status == "reserved":
                    await self._mark_unknown_receipt(db, run, existing)
                return _Rejected(_unknown())
            return await self._replay_receipt(
                db, run, existing, lease, request_hash, parameter_version
            )
        if poisoned is not None:
            return _Rejected(_unknown())
        if ordinal != run.model_requests_used or ordinal >= _MAX_REQUESTS:
            return _Rejected(_budget())
        if (
            await db.scalar(select(func.count()).select_from(RequestReceipt))
            >= MAX_RECEIPTS
        ):
            return _Rejected(_storage())
        config = self._config(now)
        amount = self._amount(config)
        month_start = now.astimezone(UTC).date().replace(day=1)
        run_ledger = await self._run_ledger(db, run, config.chat_budget_currency)
        month_ledger = await self._month_ledger(
            db, run.actor_id, month_start, config.chat_budget_currency
        )
        if run_ledger is None or month_ledger is None:
            return _Rejected(_not_configured())
        if (
            run_ledger.reserved + run_ledger.settled > config.chat_run_budget
            or month_ledger.reserved + month_ledger.settled > config.chat_month_budget
        ):
            # An existing bucket above a lowered configured bound is a
            # reconciliation anomaly, not a normal refusal.
            await self._audit(db, run, "budget_exhausted")
            return _Rejected(_budget())
        if (
            run_ledger.reserved + run_ledger.settled + amount > config.chat_run_budget
            or month_ledger.reserved + month_ledger.settled + amount
            > config.chat_month_budget
        ):
            return _Rejected(_budget())
        receipt = RequestReceipt(
            run_id=run.id,
            kind=_RECEIPT_KIND,
            ordinal=ordinal,
            request_hash=request_hash,
            parameter_version=parameter_version,
            status="reserved",
            input_cap=_INPUT_CAP,
            output_cap=_OUTPUT_CAP,
            amount=amount,
            currency=config.chat_budget_currency,
            pricing_version=config.chat_pricing_version,
            lease_owner=lease.owner,
            lease_generation=lease.generation,
            lease_attempt=lease.attempt,
            month_start=month_start,
            minor_unit=config.chat_budget_minor_unit,
            input_price_per_million=config.chat_input_price_per_million,
            output_price_per_million=config.chat_output_price_per_million,
        )
        db.add(receipt)
        run.model_requests_used += 1
        run_ledger.reserved += amount
        month_ledger.reserved += amount
        await db.flush()
        return Reservation(
            receipt_id=receipt.id,
            amount=amount,
            currency=config.chat_budget_currency,
            pricing_version=config.chat_pricing_version,
            input_cap=_INPUT_CAP,
            output_cap=_OUTPUT_CAP,
        )

    async def _replay_receipt(
        self, db, run, receipt, lease, request_hash, parameter_version
    ):
        if receipt.status == "unknown":
            return _Rejected(_unknown())
        if (
            receipt.request_hash != request_hash
            or receipt.parameter_version != parameter_version
        ):
            return _Rejected(_conflict())
        if receipt.status == "settled":
            return _Rejected(_conflict())
        if receipt.status == "dispatched":
            # The call may already have reached the provider. There is no
            # trusted result, so it becomes permanently unknown.
            await self._mark_unknown_receipt(db, run, receipt)
            return _Rejected(_unknown())
        if not self._same_lease(receipt, lease):
            # Reserved by an earlier execution that might have sent it before
            # dying. The next legal recovery identifies it as unknown.
            await self._mark_unknown_receipt(db, run, receipt)
            return _Rejected(_unknown())
        if (
            receipt.amount is None
            or receipt.currency is None
            or receipt.pricing_version is None
        ):
            await self._mark_unknown_receipt(db, run, receipt)
            return _Rejected(_unknown())
        return Reservation(
            receipt_id=receipt.id,
            amount=receipt.amount,
            currency=receipt.currency,
            pricing_version=receipt.pricing_version,
            input_cap=receipt.input_cap,
            output_cap=receipt.output_cap,
        )

    async def _dispatch(self, db, lease, receipt_id):
        await self._admit(db)
        job, now = await self._fenced(db, lease)
        run = await self._run_for_job(db, job)
        receipt = await self._receipt(db, run, receipt_id)
        if receipt.status == "unknown":
            return _Rejected(_unknown())
        if receipt.status == "settled":
            return _Rejected(_conflict())
        if not self._same_lease(receipt, lease):
            await self._mark_unknown_receipt(db, run, receipt)
            return _Rejected(_unknown())
        if receipt.status == "dispatched":
            return None
        receipt.status = "dispatched"
        receipt.dispatched_at = now
        return None

    async def _settle(self, db, lease, receipt_id, usage):
        await self._admit(db)
        job, now = await self._fenced(db, lease)
        run = await self._run_for_job(db, job)
        receipt = await self._receipt(db, run, receipt_id)
        if receipt.status == "unknown":
            return _Rejected(_unknown())
        if receipt.status == "settled":
            if (
                receipt.provider_request_id == usage.request_id
                and receipt.usage == usage.model_dump()
            ):
                return None
            return _Rejected(_conflict())
        if receipt.status == "reserved":
            # No trusted dispatch marker: releasing now could forget a charge.
            await self._mark_unknown_receipt(db, run, receipt)
            return _Rejected(_unknown())
        if not self._same_lease(receipt, lease):
            await self._mark_unknown_receipt(db, run, receipt)
            return _Rejected(_unknown())
        if (
            usage.complete is not True
            or usage.prompt_tokens > receipt.input_cap
            or usage.completion_tokens > receipt.output_cap
            or not _positive_decimal(receipt.input_price_per_million)
            or not _positive_decimal(receipt.output_price_per_million)
            or not _positive_decimal(receipt.minor_unit)
            or receipt.amount is None
            or receipt.currency is None
            or receipt.month_start is None
        ):
            await self._mark_unknown_receipt(db, run, receipt)
            return _Rejected(_unknown())
        actual = self._cost(
            usage.prompt_tokens,
            usage.completion_tokens,
            receipt.input_price_per_million,
            receipt.output_price_per_million,
            receipt.minor_unit,
        )
        run_ledger = await self._run_ledger(db, run, receipt.currency)
        month_ledger = await self._month_ledger_existing(
            db, run.actor_id, receipt.month_start, receipt.currency
        )
        if run_ledger is None or month_ledger is None:
            # Never release an amount whose bucket cannot be proven.
            await self._mark_unknown_receipt(db, run, receipt)
            return _Rejected(_unknown())
        if (
            actual > receipt.amount
            or receipt.amount > run_ledger.reserved
            or receipt.amount > month_ledger.reserved
        ):
            await self._mark_unknown_receipt(db, run, receipt)
            return _Rejected(_unknown())
        # The full reservation leaves ``reserved``; only trusted actual usage
        # becomes settled, and the confirmed unused amount is released.
        run_ledger.reserved -= receipt.amount
        run_ledger.settled += actual
        month_ledger.reserved -= receipt.amount
        month_ledger.settled += actual
        receipt.status = "settled"
        receipt.settled_at = now
        receipt.provider_request_id = usage.request_id
        receipt.usage = usage.model_dump()
        return None

    async def _mark_unknown(self, db, lease, receipt_id):
        await self._admit(db)
        job, _ = await self._fenced(db, lease)
        run = await self._run_for_job(db, job)
        receipt = await self._receipt(db, run, receipt_id)
        if receipt.status == "settled":
            return _Rejected(_conflict())
        if receipt.status == "unknown":
            return None
        await self._mark_unknown_receipt(db, run, receipt)
        return None

    async def _mark_unknown_receipt(self, db, run, receipt):
        receipt.status = "unknown"
        await self._audit(db, run, "model_call_outcome_unknown")

    async def _audit(self, db, run, reason):
        await write_audit(
            db,
            AuditEntry(
                actor_id=run.actor_id,
                action="chat.budget.reconciliation",
                resource_type="chat_run",
                resource_id=run.id,
                job_id=run.job_id,
                outcome="failure",
                reason_code=reason,
            ),
        )

    async def _admit(self, db):
        await db.execute(
            select(func.pg_advisory_xact_lock(func.hashtextextended(_BUDGET_LOCK, 0)))
        )

    async def _fenced(self, db, lease):
        job = await db.scalar(
            select(Job)
            .where(
                Job.id == lease.job_id,
                Job.kind == _KIND,
                Job.lease_owner == lease.owner,
                Job.lease_generation == lease.generation,
                Job.attempt == lease.attempt,
                Job.status == "running",
                Job.lease_expires_at > func.clock_timestamp(),
            )
            .with_for_update()
        )
        now = await db.scalar(select(func.clock_timestamp()))
        if job is None or job.lease_expires_at <= now:
            raise _lease_lost()
        return job, now

    async def _run_for_job(self, db, job):
        run = await db.scalar(
            select(AgentRun).where(AgentRun.job_id == job.id).with_for_update()
        )
        if (
            run is None
            or run.actor_id != job.requested_by
            or run.status not in ("queued", "running")
            or run.terminal_at is not None
        ):
            raise _lease_lost()
        return run

    async def _receipt(self, db, run, receipt_id):
        receipt = await db.scalar(
            select(RequestReceipt)
            .where(
                RequestReceipt.id == receipt_id,
                RequestReceipt.run_id == run.id,
            )
            .with_for_update()
        )
        if receipt is None:
            raise _lease_lost()
        return receipt

    async def _run_ledger(self, db, run, currency):
        ledger = await db.scalar(
            select(BudgetLedger).where(BudgetLedger.run_id == run.id).with_for_update()
        )
        if ledger is None:
            await db.execute(
                insert(BudgetLedger)
                .values(
                    ledger_scope="run",
                    actor_id=run.actor_id,
                    run_id=run.id,
                    currency=currency,
                )
                .on_conflict_do_nothing(constraint="uq_chat_budget_ledgers_run")
            )
            ledger = await db.scalar(
                select(BudgetLedger)
                .where(BudgetLedger.run_id == run.id)
                .with_for_update()
            )
        if ledger is None or ledger.currency != currency:
            return None
        return ledger

    async def _month_ledger(self, db, actor_id, month_start, currency):
        ledger = await db.scalar(
            select(BudgetLedger)
            .where(
                BudgetLedger.ledger_scope == "month",
                BudgetLedger.actor_id == actor_id,
                BudgetLedger.month_start == month_start,
            )
            .with_for_update()
        )
        if ledger is None:
            await db.execute(
                insert(BudgetLedger)
                .values(
                    ledger_scope="month",
                    actor_id=actor_id,
                    month_start=month_start,
                    currency=currency,
                )
                .on_conflict_do_nothing(constraint="uq_chat_budget_ledgers_month")
            )
            ledger = await db.scalar(
                select(BudgetLedger)
                .where(
                    BudgetLedger.ledger_scope == "month",
                    BudgetLedger.actor_id == actor_id,
                    BudgetLedger.month_start == month_start,
                )
                .with_for_update()
            )
        if ledger is None or ledger.currency != currency:
            return None
        return ledger

    async def _month_ledger_existing(self, db, actor_id, month_start, currency):
        ledger = await db.scalar(
            select(BudgetLedger)
            .where(
                BudgetLedger.ledger_scope == "month",
                BudgetLedger.actor_id == actor_id,
                BudgetLedger.month_start == month_start,
            )
            .with_for_update()
        )
        if ledger is None or ledger.currency != currency:
            return None
        return ledger

    def _config(self, now):
        settings = self.settings
        if settings.chat_cloud_enabled is not True:
            raise _not_configured()
        currency = settings.chat_budget_currency
        if not isinstance(currency, str) or _CURRENCY.fullmatch(currency) is None:
            raise _not_configured()
        version = settings.chat_pricing_version
        if (
            not isinstance(version, str)
            or not 1 <= len(version) <= 64
            or not version.isascii()
            or any(not 33 <= ord(char) <= 126 for char in version)
        ):
            raise _not_configured()
        valid_until = settings.chat_pricing_valid_until
        if (
            not isinstance(valid_until, datetime)
            or valid_until.tzinfo is None
            or valid_until.utcoffset() is None
            or valid_until <= now
        ):
            raise _not_configured()
        if not all(
            _positive_decimal(getattr(settings, name))
            for name in (
                "chat_run_budget",
                "chat_month_budget",
                "chat_budget_minor_unit",
                "chat_input_price_per_million",
                "chat_output_price_per_million",
            )
        ):
            raise _not_configured()
        if settings.chat_budget_minor_unit > 1:
            raise _not_configured()
        return settings

    @staticmethod
    def _amount(config):
        cost = (
            config.chat_input_price_per_million * _INPUT_CAP
            + config.chat_output_price_per_million * _OUTPUT_CAP
        ) / _MICRO
        minor = config.chat_budget_minor_unit
        return (cost / minor).to_integral_value(rounding=ROUND_CEILING) * minor

    @staticmethod
    def _cost(prompt_tokens, completion_tokens, input_price, output_price, minor):
        # Reasoning tokens are already part of completion_tokens, never a third
        # additive bucket.
        raw = (input_price * prompt_tokens + output_price * completion_tokens) / _MICRO
        return (raw / minor).to_integral_value(rounding=ROUND_CEILING) * minor

    @staticmethod
    def _same_lease(receipt, lease):
        return (
            receipt.lease_owner == lease.owner
            and receipt.lease_generation == lease.generation
            and receipt.lease_attempt == lease.attempt
        )

    @staticmethod
    def _ordinal(call_key):
        if not isinstance(call_key, str) or len(call_key) > 16:
            raise _invalid()
        match = _CALL_KEY.fullmatch(call_key)
        if match is None:
            raise _invalid()
        return int(match.group(1))

    @staticmethod
    def _hash(request_hash):
        if not isinstance(request_hash, str) or _HASH.fullmatch(request_hash) is None:
            raise _invalid()

    @staticmethod
    def _version(parameter_version):
        if (
            not isinstance(parameter_version, str)
            or not 1 <= len(parameter_version) <= 64
            or not parameter_version.isascii()
            or any(not 33 <= ord(char) <= 126 for char in parameter_version)
        ):
            raise _invalid()


__all__ = [
    "ACCOUNTED_RECEIPT_BYTES",
    "MAX_RECEIPTS",
    "MAX_RECEIPT_BYTES",
    "RETENTION",
    "BudgetService",
]
