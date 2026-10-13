from datetime import UTC, datetime
from uuid import uuid4

import pytest
from alembic import command
from pydantic import ValidationError
from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import IntegrityError
from support.job_env import job_env as job_env  # noqa: PLC0414
from support.repository_env import repo_env as repo_env  # noqa: PLC0414

from devflow.audit.models import AuditLog
from devflow.jobs.models import Job, RunEvent
from devflow.jobs.queue import Queue
from devflow.jobs.schemas import JobView

NOW = datetime(2026, 10, 13, tzinfo=UTC)
BASE_SHA, HEAD_SHA = "a" * 40, "b" * 40


def system_job(repository_id, *, kind="repository.sync", key="same"):
    return Job(
        kind=kind,
        repository_id=repository_id,
        requested_by=None,
        input={"repository_id": str(repository_id), "generation": 1},
        input_hash="a" * 64,
        idempotency_key=key,
    )


def test_upgrade_preserves_private_probe_and_system_uniqueness(empty_database):
    settings, config = empty_database
    command.upgrade(config, "0004_jobs_events")
    engine = create_engine(settings.database_url.get_secret_value())
    actor, repo, other_repo, job, event = (uuid4() for _ in range(5))
    try:
        with engine.begin() as conn:
            conn.execute(
                text("INSERT INTO users (id,github_id) VALUES (:id,42)"), {"id": actor}
            )
            conn.execute(
                text(
                    "INSERT INTO github_installations (id,installation_id) VALUES (:id,11)"
                ),
                {"id": uuid4()},
            )
            for identifier, number in [(repo, 101), (other_repo, 102)]:
                conn.execute(
                    text(
                        "INSERT INTO repositories (id,github_repository_id,installation_id,full_name,private) VALUES (:id,:number,11,'team/private',true)"
                    ),
                    {"id": identifier, "number": number},
                )
            conn.execute(
                text(
                    "INSERT INTO jobs (id,repository_id,requested_by,input,input_hash,idempotency_key,status,result,last_sequence) VALUES (:id,:repo,:actor,'{}',:hash,'legacy','succeeded',CAST(:result AS jsonb),1)"
                ),
                {
                    "id": job,
                    "repo": repo,
                    "actor": actor,
                    "hash": "a" * 64,
                    "result": '{"github_repository_id":101,"full_name":"team/private","private":true,"default_branch":"main","checked_at":"2026-10-13T00:00:00Z"}',
                },
            )
            conn.execute(
                text(
                    "INSERT INTO run_events (id,job_id,sequence,payload) VALUES (:id,:job,1,CAST(:payload AS jsonb))"
                ),
                {
                    "id": event,
                    "job": job,
                    "payload": '{"status":"succeeded","attempt":0,"error_code":null}',
                },
            )
        command.upgrade(config, "head")
        with engine.connect() as conn:
            row = (
                conn.execute(text("SELECT * FROM jobs WHERE id=:id"), {"id": job})
                .mappings()
                .one()
            )
            assert JobView.model_validate(row).result.github_repository_id == 101
            assert (
                conn.scalar(
                    text("SELECT payload->>'status' FROM run_events WHERE id=:id"),
                    {"id": event},
                )
                == "succeeded"
            )

        def insert(*, repository_id=repo, kind="repository.sync", creator=None):
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "INSERT INTO jobs (id,kind,repository_id,requested_by,input,input_hash,idempotency_key) VALUES (:id,:kind,:repo,:creator,'{}',:hash,'system-key')"
                    ),
                    {
                        "id": uuid4(),
                        "kind": kind,
                        "repo": repository_id,
                        "creator": creator,
                        "hash": "a" * 64,
                    },
                )

        insert()
        with pytest.raises(IntegrityError):
            insert()
        insert(repository_id=other_repo)
        insert(kind="repository.health")
        with pytest.raises(IntegrityError):
            insert(kind="repository.access_probe")
        with pytest.raises(IntegrityError):
            insert(creator=actor)
        command.check(config)
        with pytest.raises(RuntimeError, match="phase-two data"):
            command.downgrade(config, "0004_jobs_events")
    finally:
        engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "code",
    [
        "resource_not_found",
        "permission_revoked",
        "permission_missing",
        "github_installation_unavailable",
        "github_response_too_large",
        "source_limit",
        "budget_exhausted",
        "version_changed",
        "version_unavailable",
    ],
)
async def test_system_denial_reaches_terminal_state(job_env, code):
    env = job_env
    async with env.factory.begin() as session:
        job = system_job(env.repository.id)
        session.add(job)
        await session.flush()
        job_id = job.id
    queue = Queue(env.factory)
    lease = await queue.claim(uuid4())
    assert await queue.fail(lease, code, False) is True
    async with env.factory() as session:
        job = await session.get(Job, job_id)
        assert job.status == "failed" and job.error_code == code
        assert job.lease_owner is job.lease_expires_at is None
        events = list(
            await session.scalars(
                select(RunEvent)
                .where(RunEvent.job_id == job_id)
                .order_by(RunEvent.sequence)
            )
        )
        assert events[-1].payload == {
            "status": "failed",
            "attempt": 1,
            "error_code": code,
        }
        audits = list(
            await session.scalars(select(AuditLog).where(AuditLog.job_id == job_id))
        )
        assert any(
            row.action == "job.failed" and row.reason_code == code for row in audits
        )
        assert all(row.actor_id is None for row in audits)
    from devflow.errors import AppError

    with pytest.raises(AppError):
        await env.jobs.get_visible(env.actor, job_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["repository.sync", "repository.health"])
async def test_queue_validates_result_for_actual_job_kind(job_env, kind):
    from devflow.jobs.schemas import HealthResult, ProbeResult, SyncResult

    env = job_env
    async with env.factory.begin() as session:
        job = system_job(env.repository.id, kind=kind)
        session.add(job)
        await session.flush()
        job_id = job.id
    queue = Queue(env.factory)
    lease = await queue.claim(uuid4())
    wrong = ProbeResult(
        github_repository_id=101,
        full_name="team/private",
        private=True,
        default_branch="main",
        checked_at=NOW,
    )
    with pytest.raises(ValidationError):
        await queue.complete(lease, wrong)
    async with env.factory() as session:
        assert (await session.get(Job, job_id)).status == "running"
    result = (
        SyncResult(repository_id=env.repository.id, generation=1, continued=True)
        if kind == "repository.sync"
        else HealthResult(snapshot_id=uuid4(), generation=1)
    )
    assert await queue.complete(lease, result)
    async with env.factory() as session:
        job = await session.get(Job, job_id)
        assert job.result == result.model_dump(mode="json")
        assert job.status == "succeeded"
        with pytest.raises(ValidationError):
            JobView.model_validate(job)


@pytest.mark.asyncio
async def test_versions_preserve_old_ci_attempt_and_pr_files(job_env):
    from devflow.facts.models import Fact
    from devflow.facts.schemas import FactInput, ci_run_version_key, pr_file_version_key

    env = job_env
    values = []
    for attempt in (1, 2):
        values.append(
            FactInput(
                repository_id=env.repository.id,
                kind="ci_run",
                source_id=55,
                version_key=ci_run_version_key(attempt=attempt),
                state="completed",
                source_updated_at=NOW,
                observed_at=NOW,
                source_url="https://github.com/team/private/actions/runs/55",
                payload={
                    "attempt": attempt,
                    "workflow_id": 88,
                    "workflow_path": ".github/workflows/ci.yml",
                    "event": "push",
                    "branch": "main",
                    "head_sha": HEAD_SHA,
                    "status": "completed",
                    "conclusion": "failure" if attempt == 1 else "success",
                    "created_at": NOW,
                    "updated_at": NOW,
                },
            )
        )
    for head in (HEAD_SHA, "c" * 40):
        for filename in ("src/a.py", "src/b.py"):
            values.append(
                FactInput(
                    repository_id=env.repository.id,
                    kind="pr_file",
                    source_id=100,
                    parent_kind="pull",
                    parent_source_id=100,
                    version_key=pr_file_version_key(
                        base_sha=BASE_SHA, head_sha=head, filename=filename
                    ),
                    source_updated_at=NOW,
                    observed_at=NOW,
                    source_url="https://github.com/team/private/pull/1/files",
                    payload={
                        "base_sha": BASE_SHA,
                        "head_sha": head,
                        "filename": filename,
                        "status": "modified",
                        "additions": 1,
                        "deletions": 0,
                        "changes": 1,
                        "patch": None,
                        "patch_status": "missing",
                    },
                )
            )
    async with env.factory.begin() as session:
        session.add_all(Fact.from_input(value) for value in values)
    async with env.factory() as session:
        facts = list(await session.scalars(select(Fact).order_by(Fact.version_key)))
        assert len(facts) == 6
        assert {
            fact.payload["conclusion"] for fact in facts if fact.kind == "ci_run"
        } == {"success", "failure"}
        assert {
            fact.payload["filename"] for fact in facts if fact.kind == "pr_file"
        } == {"src/a.py", "src/b.py"}
    with pytest.raises(IntegrityError):
        async with env.factory.begin() as session:
            session.add(Fact.from_input(values[0]))


def test_fact_dtos_reject_raw_keys_and_invalid_identity():
    from devflow.facts.schemas import (
        FactInput,
        PullPayload,
        ci_run_version_key,
        pr_file_version_key,
    )

    pull = {
        "issue_id": 10,
        "title": "PR",
        "body": None,
        "state": "open",
        "base_repository_id": 101,
        "head_repository_id": None,
        "base_ref": "main",
        "head_ref": "work",
        "base_sha": BASE_SHA,
        "head_sha": HEAD_SHA,
        "draft": False,
        "merged": False,
        "merge_commit_sha": None,
    }
    assert PullPayload.model_validate(pull).issue_id == 10
    for changes in ({"token": "secret"}, {"head_sha": "main"}, {"issue_id": 0}):
        with pytest.raises(ValidationError):
            PullPayload.model_validate({**pull, **changes})
    good = {
        "repository_id": uuid4(),
        "kind": "pull",
        "source_id": 20,
        "number": 1,
        "observed_at": NOW,
        "source_updated_at": NOW,
        "source_url": "https://github.com/team/repo/pull/1",
        "payload": pull,
    }
    assert FactInput(**good).version_key == ""
    for changes in (
        {"source_id": 0},
        {"observed_at": NOW.replace(tzinfo=None)},
        {"source_url": "javascript:alert(1)"},
        {"parent_source_id": 10},
        {"payload": {**pull, "raw": {}}},
    ):
        with pytest.raises(ValidationError):
            FactInput(**{**good, **changes})
    with pytest.raises(ValidationError):
        ci_run_version_key(attempt=0)
    assert pr_file_version_key(
        base_sha=BASE_SHA, head_sha=HEAD_SHA, filename="a"
    ) != pr_file_version_key(base_sha=BASE_SHA, head_sha=HEAD_SHA, filename="b")


def test_checkpoint_dto_preserves_nested_progress_and_rejects_raw_keys():
    from devflow.sync.schemas import SyncCheckpoint

    data = {
        "stream": "pulls",
        "mode": "backfill",
        "page": 2,
        "window": {"since": NOW, "until": NOW},
        "pending_targets": [{"kind": "pull", "source_id": 100, "number": 1}],
        "target": {"kind": "pull", "source_id": 100, "number": 1},
        "stage": "files",
        "child_page": 3,
        "captured_sha": {"base": BASE_SHA, "head": HEAD_SHA},
        "recaptures": 2,
        "captured_sequence": 5,
    }
    checkpoint = SyncCheckpoint.model_validate(data)
    assert (
        SyncCheckpoint.model_validate_json(checkpoint.model_dump_json()) == checkpoint
    )
    for changes in (
        {"page": 0},
        {"recaptures": 4},
        {"token": "secret"},
        {"pending_targets": [{"kind": "pull", "source_id": 0}]},
        {"captured_sha": {"base": "main", "head": HEAD_SHA}},
    ):
        with pytest.raises(ValidationError):
            SyncCheckpoint.model_validate({**data, **changes})


@pytest.mark.asyncio
async def test_sync_and_collection_constraints(job_env):
    from devflow.facts.models import FactCollection
    from devflow.facts.schemas import CollectionInput
    from devflow.health.models import HealthSnapshot
    from devflow.sync.models import RepositorySync, SyncCursor
    from devflow.sync.schemas import SyncCheckpoint

    env = job_env
    async with env.factory.begin() as session:
        sync = RepositorySync(repository_id=env.repository.id)
        cursor = SyncCursor(
            repository_id=env.repository.id,
            stream="issues",
            checkpoint=SyncCheckpoint(stream="issues").model_dump(mode="json"),
        )
        collection = CollectionInput(
            repository_id=env.repository.id,
            parent_kind="pull",
            parent_source_id=100,
            collection="reviews",
            observed_at=NOW,
            complete=False,
            reason="source_limit",
        )
        session.add_all(
            [
                sync,
                cursor,
                FactCollection.from_input(collection),
                HealthSnapshot(
                    repository_id=env.repository.id,
                    generation=1,
                    rule_version="health-v1",
                    manifest={},
                    result={},
                ),
            ]
        )
    async with env.factory() as session:
        sync = await session.get(RepositorySync, env.repository.id)
        assert sync.requested_sequence == sync.processed_sequence == 0
        assert sync.health_generation == sync.published_health_generation == 0
        assert sync.status == "idle"
    for row in [
        SyncCursor(
            repository_id=env.repository.id,
            stream="issues",
            checkpoint=SyncCheckpoint(stream="issues").model_dump(mode="json"),
        ),
        FactCollection.from_input(collection),
        HealthSnapshot(
            repository_id=env.repository.id,
            generation=1,
            rule_version="health-v1",
            manifest={},
            result={},
        ),
    ]:
        with pytest.raises((IntegrityError, ValidationError)):
            async with env.factory.begin() as session:
                session.add(row)
    for updates in [
        "processed_sequence=1",
        "published_health_generation=1",
        "request_count=-1",
    ]:
        with pytest.raises(IntegrityError):
            async with env.factory.begin() as session:
                await session.execute(
                    text(
                        f"UPDATE repository_syncs SET {updates} WHERE repository_id=:id"
                    ),
                    {"id": env.repository.id},
                )
    with pytest.raises(ValidationError):
        CollectionInput(
            repository_id=env.repository.id,
            parent_kind="pull",
            parent_source_id=100,
            collection="reviews",
            observed_at=NOW,
            complete=True,
            reason="source_limit",
        )


@pytest.mark.asyncio
async def test_sync_demand_and_per_attempt_budget_persist(job_env):
    from devflow.sync.models import SyncDemand
    from devflow.sync.schemas import SyncDemandInput

    env = job_env
    first = SyncDemandInput(
        repository_id=env.repository.id,
        sequence=1,
        reason="webhook",
        target={"kind": "pull", "source_id": 100, "number": 1},
    )
    second = SyncDemandInput(
        repository_id=env.repository.id,
        sequence=2,
        reason="backfill",
        backfill={"since": NOW, "until": NOW},
    )
    async with env.factory.begin() as session:
        job = system_job(env.repository.id)
        job.request_count = 2
        job.attempt_started_at = NOW
        session.add_all(
            [job, SyncDemand.from_input(first), SyncDemand.from_input(second)]
        )
        await session.flush()
        job_id = job.id
    async with env.factory() as session:
        rows = list(
            await session.scalars(select(SyncDemand).order_by(SyncDemand.sequence))
        )
        assert [(row.sequence, row.reason) for row in rows] == [
            (1, "webhook"),
            (2, "backfill"),
        ]
        assert rows[0].target["source_id"] == 100
        assert rows[1].backfill["since"] == "2026-10-13T00:00:00Z"
        job = await session.get(Job, job_id)
        assert job.request_count == 2 and job.attempt_started_at == NOW
    with pytest.raises(IntegrityError):
        async with env.factory.begin() as session:
            session.add(SyncDemand.from_input(first))
    with pytest.raises(IntegrityError):
        async with env.factory.begin() as session:
            await session.execute(
                text("UPDATE jobs SET request_count=-1 WHERE id=:id"), {"id": job_id}
            )
    for changes in [
        {"sequence": 0},
        {"target": {"kind": "pull", "source_id": 100, "raw": "secret"}},
        {"backfill": {"since": NOW.replace(tzinfo=None), "until": NOW}},
    ]:
        with pytest.raises(ValidationError):
            SyncDemandInput.model_validate({**first.model_dump(), **changes})


@pytest.mark.parametrize(
    "kind,payload",
    [
        ("issue", {"title": "I", "state": "future_state"}),
        ("review", {"state": "FUTURE_REVIEW"}),
        ("review_comment", {"side": "future_side"}),
        (
            "pr_file",
            {
                "base_sha": BASE_SHA,
                "head_sha": HEAD_SHA,
                "filename": "a",
                "status": "future_file",
                "additions": 0,
                "deletions": 0,
                "changes": 0,
                "patch_status": "missing",
            },
        ),
        (
            "ci_run",
            {
                "attempt": 1,
                "workflow_id": 88,
                "event": "push",
                "head_sha": HEAD_SHA,
                "created_at": NOW,
                "updated_at": NOW,
                "status": "future_status",
                "conclusion": "future_conclusion",
            },
        ),
        (
            "ci_job",
            {
                "run_id": 55,
                "attempt": 1,
                "head_sha": HEAD_SHA,
                "name": "test",
                "status": "future_status",
                "steps": [{"number": 1, "name": "test", "status": "future_status"}],
            },
        ),
    ],
)
def test_unknown_provider_enum_is_explicit_and_malformed_enum_is_rejected(
    kind, payload
):
    from devflow.facts.schemas import validate_fact_payload

    parsed = validate_fact_payload(kind, payload).model_dump(mode="json")
    enum_key = (
        "state"
        if kind in {"issue", "review"}
        else "side"
        if kind == "review_comment"
        else "status"
    )
    assert parsed[enum_key] == "unknown"
    if kind == "ci_run":
        assert parsed["conclusion"] == "unknown"
    if kind == "ci_job":
        assert parsed["steps"][0]["status"] == "unknown"
    with pytest.raises(ValidationError):
        validate_fact_payload(kind, {**payload, enum_key: {"raw": "secret"}})


def test_fact_orm_rejects_unwhitelisted_payload_and_parent_identity():
    from devflow.facts.models import Fact
    from devflow.facts.schemas import FactInput

    with pytest.raises(ValidationError):
        Fact(kind="comment", payload={"body": "hello", "headers": {"token": "secret"}})
    good = {
        "repository_id": uuid4(),
        "kind": "comment",
        "source_id": 55,
        "parent_kind": "pull",
        "parent_source_id": 100,
        "source_url": "https://github.com/team/repo/pull/1",
        "observed_at": NOW,
        "payload": {"body": "hello"},
    }
    assert FactInput(**good).parent_source_id == 100
    with pytest.raises(ValidationError):
        FactInput(
            **{
                **good,
                "kind": "review",
                "parent_kind": "issue",
                "payload": {"state": "APPROVED", "commit_id": HEAD_SHA},
            }
        )


@pytest.mark.asyncio
async def test_system_denial_audit_failure_rolls_back_terminal_state(job_env):
    from sqlalchemy.exc import DBAPIError

    env = job_env
    async with env.factory.begin() as session:
        job = system_job(env.repository.id)
        session.add(job)
        await session.flush()
        job_id = job.id
    queue = Queue(env.factory)
    lease = await queue.claim(uuid4())
    async with env.factory.begin() as session:
        await session.execute(
            text(
                "CREATE FUNCTION reject_system_denial() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF NEW.action = 'permission.denied' THEN RAISE EXCEPTION 'audit unavailable'; END IF; RETURN NEW; END $$"
            )
        )
        await session.execute(
            text(
                "CREATE TRIGGER reject_system_denial BEFORE INSERT ON audit_logs FOR EACH ROW EXECUTE FUNCTION reject_system_denial()"
            )
        )
    with pytest.raises(DBAPIError):
        await queue.fail(lease, "permission_revoked", False)
    async with env.factory() as session:
        job = await session.get(Job, job_id)
        assert job.status == "running" and job.last_sequence == 1
        assert job.error_code is None
        assert list(
            await session.scalars(
                select(RunEvent.payload).where(RunEvent.job_id == job_id)
            )
        ) == [{"status": "running", "attempt": 1, "error_code": None}]


@pytest.mark.asyncio
async def test_database_rejects_child_fact_without_canonical_parent(job_env):
    from devflow.facts.models import Fact

    with pytest.raises(IntegrityError):
        async with job_env.factory.begin() as session:
            session.add(
                Fact(
                    repository_id=job_env.repository.id,
                    kind="comment",
                    source_id=55,
                    source_url="https://github.com/team/repo/issues/1",
                    observed_at=NOW,
                    payload={"body": "hello"},
                )
            )
