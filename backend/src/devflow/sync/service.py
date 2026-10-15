"""Short, ordered transactions fence every reservation and page publication."""

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import distinct_on, insert

from devflow.errors import AppError
from devflow.facts.models import Fact, FactCollection
from devflow.facts.schemas import CollectionInput, FactInput
from devflow.jobs.models import Job
from devflow.jobs.queue import Queue
from devflow.jobs.schemas import SyncResult
from devflow.sync.models import SyncCursor, SyncDemand
from devflow.sync.scheduler import enqueue
from devflow.sync.schemas import BackfillRange, SyncCheckpoint, SyncTarget, SyncWindow
from devflow.sync.scope import (
    BackgroundScope,
    authorized,
    lock_scope,
    scope_of,
    transaction,
)


class LeaseLost(Exception):
    """The executor must stop without a second publication attempt."""


class UnitComplete(Exception):
    """Normal request/time budget boundary; persist a successful continuation."""


@dataclass(frozen=True, slots=True)
class SyncWork:
    scope: BackgroundScope
    generation: int
    captured_sequence: int
    cursor: SyncCheckpoint
    observed_at: datetime
    known_targets: tuple[SyncTarget, ...] = ()
    known_targets_complete: bool = True


class SyncService:
    def __init__(self, factory, settings):
        self.factory, self.settings = factory, settings

    async def _locked(self, db, lease, work=None):
        repository_id = await db.scalar(
            select(Job.repository_id).where(Job.id == lease.job_id)
        )
        if repository_id is None:
            return None
        rows = await lock_scope(db, repository_id)
        if rows is None:
            return None
        installation, repository, state = rows
        job, now = await Queue(self.factory)._owned(db, lease)
        if (
            job is None
            or job.kind != "repository.sync"
            or state is None
            or state.active_job_id != job.id
            or state.generation != job.input.get("generation")
            or not authorized(
                installation, repository, self.settings, work.scope if work else None
            )
            or job.input.get("installation_id") != installation.installation_id
            or job.input.get("installation_version") != installation.auth_version
            or job.input.get("repository_version") != repository.auth_version
            or (
                work is not None
                and (
                    work.generation != state.generation
                    or work.captured_sequence != job.input.get("captured_sequence")
                    or work.cursor.stream not in job.input["streams"]
                )
            )
        ):
            return None
        return installation, repository, state, job, now

    async def _initialize(self, db, rows):
        _, repository, state, job, _now = rows
        sequence = job.input["captured_sequence"]
        demands = list(
            await db.scalars(
                select(SyncDemand)
                .where(
                    SyncDemand.repository_id == repository.id,
                    SyncDemand.sequence > state.processed_sequence,
                    SyncDemand.sequence <= sequence,
                )
                .order_by(SyncDemand.sequence)
            )
        )
        targeted = bool(demands) and all(d.target is not None for d in demands)
        backfill = (
            BackfillRange.model_validate(demands[0].backfill)
            if demands and demands[0].backfill
            else None
        )
        targets = list(
            dict.fromkeys(
                SyncTarget.model_validate(d.target) for d in demands if d.target
            )
        )
        for stream, kind in (
            ("issues", "issue"),
            ("pulls", "pull"),
            ("actions", "ci_run"),
        ):
            if stream not in job.input["streams"]:
                continue
            row = await db.get(SyncCursor, (repository.id, stream))
            if row and row.checkpoint["captured_sequence"] == sequence:
                continue
            mode = (
                "backfill"
                if backfill
                else "targeted"
                if targeted
                else "reconcile"
                if any(d.reason == "reconcile" for d in demands)
                else "initial"
                if row is None or row.completed_watermark is None
                else "incremental"
            )
            days = (
                self.settings.sync_actions_days
                if stream == "actions"
                else self.settings.sync_history_days
            )
            since = state.round_started_at - timedelta(days=days)
            if mode == "incremental" and row.completed_watermark:
                since = max(
                    since,
                    row.completed_watermark
                    - timedelta(seconds=self.settings.sync_overlap_seconds),
                )
            checkpoint = SyncCheckpoint(
                stream=stream,
                mode=mode,
                window=SyncWindow(since=backfill.since, until=backfill.until)
                if backfill
                else SyncWindow(since=since, until=state.round_started_at),
                pending_targets=[t for t in targets if t.kind == kind],
                captured_sequence=sequence,
                stage="enumerate",
            )
            if row is None:
                row = SyncCursor(
                    repository_id=repository.id,
                    stream=stream,
                    checkpoint=checkpoint.model_dump(mode="json"),
                )
                db.add(row)
            else:
                row.checkpoint = checkpoint.model_dump(mode="json")
            row.status = "pending"
            row.error_code = None
        await db.flush()

    async def snapshot(self, lease) -> SyncWork:
        async with transaction(self.factory) as db:
            rows = await self._locked(db, lease)
            if rows is None:
                raise LeaseLost
            await self._initialize(db, rows)
            installation, repository, state, job, now = rows
            # Fixed domain order: issues -> pulls -> actions.
            cursors = list(
                await db.scalars(
                    select(SyncCursor).where(
                        SyncCursor.repository_id == repository.id,
                        SyncCursor.stream.in_(job.input["streams"]),
                    )
                )
            )
            cursor = next(
                (
                    c
                    for stream in ("issues", "pulls", "actions")
                    for c in cursors
                    if c.stream == stream
                    and c.checkpoint["stage"] != "done"
                    and c.status != "complete"
                ),
                None,
            )
            if cursor is None:
                raise LeaseLost
            checkpoint = SyncCheckpoint.model_validate(cursor.checkpoint)
            known = []
            if checkpoint.stream == "actions" and not checkpoint.known_complete:
                # Collapse attempts to stable run IDs, then hydrate at most one page.
                latest = (
                    select(Fact.id)
                    .where(
                        Fact.repository_id == repository.id,
                        Fact.kind == "ci_run",
                        Fact.source_id > checkpoint.known_after_id,
                    )
                    .ext(distinct_on(Fact.source_id))
                    .order_by(
                        Fact.source_id, Fact.payload["attempt"].as_integer().desc()
                    )
                    .subquery()
                )
                facts = list(
                    await db.scalars(
                        select(Fact)
                        .where(
                            Fact.id.in_(select(latest.c.id)),
                            Fact.unavailable.is_(False),
                            Fact.source_id > checkpoint.known_after_id,
                            (
                                (Fact.payload["status"].astext != "completed")
                                | (
                                    Fact.source_updated_at
                                    >= state.round_started_at
                                    - timedelta(days=self.settings.sync_actions_days)
                                )
                            ),
                        )
                        .order_by(Fact.source_id)
                        .limit(101)
                    )
                )
                known = [
                    SyncTarget(
                        kind="ci_run",
                        source_id=f.source_id,
                        attempt=f.payload["attempt"],
                    )
                    for f in facts
                ]
            return SyncWork(
                scope_of(installation, repository),
                state.generation,
                job.input["captured_sequence"],
                checkpoint,
                now,
                tuple(known[:100]),
                len(known) <= 100,
            )

    async def reserve_request(self, lease, work) -> None:
        async with transaction(self.factory) as db:
            rows = await self._locked(db, lease, work)
            if rows is None:
                raise LeaseLost
            _, _, state, job, now = rows
            if (
                state.request_count >= self.settings.sync_round_requests
                or now + timedelta(seconds=10)
                > state.round_started_at
                + timedelta(seconds=self.settings.sync_round_seconds)
            ):
                raise AppError("budget_exhausted", 409, "Sync round budget exhausted")
            if job.request_count >= self.settings.sync_unit_requests or now + timedelta(
                seconds=10
            ) > job.attempt_started_at + timedelta(
                seconds=self.settings.sync_unit_seconds
            ):
                raise UnitComplete
            state.request_count += 1
            job.request_count += 1

    async def commit_page(
        self,
        lease,
        work,
        *,
        facts,
        collections,
        checkpoint,
        complete,
        outcome="complete",
        reason=None,
        unavailable=(),
    ) -> bool:
        if outcome not in {"complete", "partial", "permission_missing"} or (
            (outcome == "complete") != (reason is None)
        ):
            raise ValueError("Stream outcome requires a matching coverage reason")
        checkpoint = SyncCheckpoint.model_validate(checkpoint)
        if complete:
            checkpoint = checkpoint.model_copy(update={"stage": "done"})
        facts = [FactInput.model_validate(f) for f in facts]
        collections = [CollectionInput.model_validate(c) for c in collections]
        unavailable = [SyncTarget.model_validate(t) for t in unavailable]
        if unavailable and (
            work.cursor.stage != "missing_verify" or unavailable != [work.cursor.target]
        ):
            raise ValueError(
                "Unavailable target requires independently confirmed proof"
            )
        if (
            checkpoint.stream != work.cursor.stream
            or checkpoint.captured_sequence != work.captured_sequence
        ):
            raise ValueError("Checkpoint does not belong to captured stream")
        if any(
            value.repository_id != work.scope.repository_id
            for value in [*facts, *collections]
        ):
            raise ValueError("Fact does not belong to background scope")
        async with transaction(self.factory) as db:
            rows = await self._locked(db, lease, work)
            if rows is None:
                return False
            installation, repository, state, job, now = rows
            cursor = await db.get(SyncCursor, (repository.id, work.cursor.stream))
            if cursor is None or cursor.checkpoint != work.cursor.model_dump(
                mode="json"
            ):
                return False
            if (
                checkpoint.target_started_at is not None
                and checkpoint.target_started_at != work.cursor.target_started_at
                and checkpoint.target_started_at != work.observed_at
            ):
                # A collector may use work.observed_at to start the target; never accept provider time.
                raise ValueError("Target fence requires database observation time")
            for target in unavailable:
                condition = [
                    Fact.repository_id == repository.id,
                    Fact.kind == target.kind,
                ]
                if target.source_id is not None:
                    condition.append(Fact.source_id == target.source_id)
                else:
                    condition.append(Fact.number == target.number)
                await db.execute(
                    update(Fact)
                    .where(*condition)
                    .values(unavailable=True, observed_at=now)
                )
            for fact in facts:
                values = fact.model_dump()
                values["payload"] = fact.payload.model_dump(mode="json")
                values["observed_at"] = now
                statement = insert(Fact).values(**values)
                await db.execute(
                    statement.on_conflict_do_update(
                        constraint="uq_facts_source_version",
                        set_={
                            key: getattr(statement.excluded, key)
                            for key in values
                            if key
                            not in {"repository_id", "kind", "source_id", "version_key"}
                        },
                    )
                )
            for collection in collections:
                values = collection.model_dump()
                values["observed_at"] = now
                statement = insert(FactCollection).values(**values)
                await db.execute(
                    statement.on_conflict_do_update(
                        constraint="uq_fact_collections_parent_version",
                        set_={
                            key: getattr(statement.excluded, key)
                            for key in ("complete", "reason", "observed_at")
                        },
                    )
                )
                if collection.complete and work.cursor.target_started_at:
                    await self._remove_missing(
                        db, collection, work.cursor.target_started_at
                    )
            cursor.checkpoint = checkpoint.model_dump(mode="json")
            cursor.status = outcome if complete else "running"
            cursor.error_code = reason if complete else None
            if complete and outcome == "complete":
                cursor.last_success_at = now
                if checkpoint.window and checkpoint.mode != "targeted":
                    window = checkpoint.window
                    if cursor.covered_from is None or cursor.covered_until is None:
                        cursor.covered_from, cursor.covered_until = (
                            window.since,
                            window.until,
                        )
                    elif (
                        window.since <= cursor.covered_until
                        and window.until >= cursor.covered_from
                    ):
                        cursor.covered_from = min(cursor.covered_from, window.since)
                        cursor.covered_until = max(cursor.covered_until, window.until)
                    elif window.until > cursor.covered_until:
                        # One range can attest only one contiguous interval. Never
                        # claim the unsampled gap between disjoint backfills.
                        cursor.covered_from, cursor.covered_until = (
                            window.since,
                            window.until,
                        )
                    cursor.completed_watermark = (
                        max(cursor.completed_watermark, checkpoint.window.until)
                        if cursor.completed_watermark
                        else checkpoint.window.until
                    )
            if complete:
                await db.flush()
                unfinished = await db.scalar(
                    select(func.count())
                    .select_from(SyncCursor)
                    .where(
                        SyncCursor.repository_id == repository.id,
                        SyncCursor.stream.in_(job.input["streams"]),
                        SyncCursor.checkpoint["stage"].astext != "done",
                    )
                )
                if not unfinished:
                    await self._succeed(db, job, state, now, False)
                    state.processed_sequence = work.captured_sequence
                    await db.execute(
                        delete(SyncDemand).where(
                            SyncDemand.repository_id == repository.id,
                            SyncDemand.sequence <= work.captured_sequence,
                        )
                    )
                    state.active_job_id = None
                    state.status = "completed"
                    state.next_due_at = now + timedelta(
                        seconds=self.settings.sync_interval_seconds
                    )
                    completed_cursors = list(
                        await db.scalars(
                            select(SyncCursor).where(
                                SyncCursor.repository_id == repository.id,
                                SyncCursor.stream.in_(job.input["streams"]),
                            )
                        )
                    )
                    if all(c.status == "complete" for c in completed_cursors) and any(
                        c.checkpoint["mode"] in {"initial", "reconcile"}
                        for c in completed_cursors
                    ):
                        state.last_reconciled_at = now
                    await enqueue(db, state, installation, repository)
            return True

    @staticmethod
    async def _remove_missing(db, collection, started):
        kinds = {
            "comments": "comment",
            "reviews": "review",
            "review_comments": "review_comment",
            "files": "pr_file",
            "jobs": "ci_job",
        }
        if collection.collection not in kinds:
            return
        condition = [
            Fact.repository_id == collection.repository_id,
            Fact.parent_kind == collection.parent_kind,
            Fact.parent_source_id == collection.parent_source_id,
            Fact.kind == kinds[collection.collection],
            Fact.observed_at < started,
        ]
        if collection.collection == "files":
            parts = collection.version_key.split(":")
            if len(parts) != 2 or any(len(p) != 40 for p in parts):
                raise ValueError("File collection requires base:head version")
            condition += [
                Fact.payload["base_sha"].astext == parts[0],
                Fact.payload["head_sha"].astext == parts[1],
            ]
        elif collection.collection == "jobs":
            if (
                not collection.version_key.isdecimal()
                or int(collection.version_key) < 1
            ):
                raise ValueError("Jobs collection requires attempt version")
            condition.append(
                Fact.payload["attempt"].as_integer() == int(collection.version_key)
            )
        else:
            condition.append(Fact.version_key == collection.version_key)
        await db.execute(update(Fact).where(*condition).values(unavailable=True))

    async def continue_job(self, lease, work) -> bool:
        async with transaction(self.factory) as db:
            rows = await self._locked(db, lease, work)
            if rows is None:
                return False
            installation, repository, state, job, now = rows
            await self._succeed(db, job, state, now, True)
            await enqueue(db, state, installation, repository, continued=True)
            return True

    @staticmethod
    async def _succeed(db, job, state, now, continued):
        job.status = "succeeded"
        job.result = SyncResult(
            repository_id=job.repository_id,
            generation=state.generation,
            continued=continued,
        ).model_dump(mode="json")
        job.error_code = None
        job.lease_owner = job.lease_expires_at = None
        job.updated_at = now
        await Queue._transition(db, job, "job.succeeded")
