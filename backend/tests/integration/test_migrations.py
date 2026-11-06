import pytest
from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy import text
from support.repository_env import repo_env as repo_env  # noqa: PLC0414


@pytest.fixture
def tmp_path(workspace_tmp_path):
    """Opt in to newly owned workspace files without changing Windows ACLs."""
    return workspace_tmp_path


def test_migrate_empty_database(empty_database):
    from sqlalchemy import create_engine

    settings, config = empty_database
    command.upgrade(config, "head")
    engine = create_engine(settings.database_url.get_secret_value())
    try:
        with engine.connect() as connection:
            current_revision = connection.scalar(
                text("SELECT version_num FROM alembic_version")
            )
            script_head = ScriptDirectory.from_config(config).get_current_head()
            assert current_revision == script_head
            sequence = connection.execute(
                text(
                    "SELECT seqcache, seqcycle FROM pg_sequence WHERE seqrelid = 'auth_change_order_seq'::regclass"
                )
            ).one()
            assert sequence == (1, False)
        command.check(config)
        command.downgrade(config, "base")
        with engine.connect() as connection:
            assert (
                connection.scalar(text("SELECT to_regclass('auth_change_order_seq')"))
                is None
            )
        command.upgrade(config, "head")
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    "url",
    [
        "postgresql+psycopg://devflow_test:x@127.0.0.1:55439/devflow",
        "postgresql+psycopg://devflow_test:x@remote:55439/devflow_test",
        "postgresql+psycopg://devflow_test:x@127.0.0.1:55439/devflow_test?options=-csearch_path=public",
    ],
)
def test_fixture_rejects_non_test_database(url):
    from conftest import validated_test_url

    with pytest.raises(ValueError):
        validated_test_url(url)


@pytest.mark.asyncio
async def test_auth_change_time_survives_profile_updates(db_factory):
    from devflow.auth.models import User

    async with db_factory.begin() as session:
        user = User(github_id=123, login="before")
        session.add(user)
        await session.flush()
        cutoff = user.auth_changed_at
        order = user.auth_changed_order
        assert cutoff.utcoffset().total_seconds() == 0
        user_id = user.id
    async with db_factory.begin() as session:
        user = await session.get(User, user_id)
        user.login = "after"
    async with db_factory() as session:
        user = await session.get(User, user_id)
        assert user.login == "after" and user.auth_changed_at == cutoff
        assert user.auth_changed_order == order


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "evidence", ["content_receipt", "command_receipt", "version_hint"]
)
async def test_content_downgrade_preserves_new_evidence(
    db_factory, migrated_database, evidence
):
    from uuid import uuid4

    from devflow.auth.models import User
    from devflow.github.models import GitHubInstallation, WebhookDelivery
    from devflow.repositories.models import Repository
    from devflow.sync.models import RepositoryCommandReceipt, RepositorySync

    async with db_factory.begin() as db:
        if evidence == "content_receipt":
            db.add(
                WebhookDelivery(
                    delivery_id=uuid4(), event_name="push", status="ignored"
                )
            )
        else:
            user = User(github_id=42)
            db.add_all([user, GitHubInstallation(installation_id=11)])
            await db.flush()
            repo = Repository(
                github_repository_id=101,
                installation_id=11,
                full_name="team/private",
                private=True,
            )
            db.add(repo)
            await db.flush()
            if evidence == "version_hint":
                db.add(RepositorySync(repository_id=repo.id, version_hint_sequence=1))
            else:
                db.add(
                    RepositoryCommandReceipt(
                        actor_id=user.id,
                        repository_id=repo.id,
                        operation="sync.refresh",
                        idempotency_key="retained",
                        payload_hash="a" * 64,
                        demand_sequence=1,
                    )
                )
    with pytest.raises(RuntimeError, match="phase-two data"):
        command.downgrade(migrated_database[1], "0005_data_foundation")
    async with db_factory() as db:
        assert (
            await db.scalar(text("SELECT version_num FROM alembic_version"))
            == ScriptDirectory.from_config(migrated_database[1]).get_current_head()
        )
        table = {
            "content_receipt": "webhook_deliveries",
            "command_receipt": "repository_command_receipts",
            "version_hint": "repository_syncs",
        }[evidence]
        assert await db.scalar(text(f"SELECT count(*) FROM {table}")) == 1


@pytest.mark.asyncio
async def test_health_downgrade_refuses_durable_demand(repo_env, migrated_database):
    from devflow.health.service import HealthService
    from devflow.sync.models import RepositorySync

    env = repo_env
    repo = await env.service.register(env.actor, 11, 101)
    await HealthService(env.factory, env.policy, env.settings).request(
        env.actor, repo.id, "migration"
    )
    with pytest.raises(RuntimeError, match="Cannot discard phase-two data"):
        command.downgrade(migrated_database[1], "0006_content_webhooks")
    async with env.factory() as db:
        assert (await db.get(RepositorySync, repo.id)).health_requested_sequence == 1
        assert (
            await db.scalar(text("SELECT version_num FROM alembic_version"))
            == ScriptDirectory.from_config(migrated_database[1]).get_current_head()
        )


@pytest.mark.asyncio
async def test_sync_repair_downgrade_preserves_deadline(repo_env, migrated_database):
    from datetime import UTC, datetime

    from devflow.sync.models import RepositorySync

    env = repo_env
    repo = await env.service.register(env.actor, 11, 101)
    due = datetime(2030, 1, 1, tzinfo=UTC)
    async with env.factory.begin() as db:
        state = await db.get(RepositorySync, repo.id)
        state.next_reconcile_at = due
    with pytest.raises(RuntimeError, match="Cannot discard sync repair deadlines"):
        command.downgrade(migrated_database[1], "0007_health_requests")
    async with env.factory() as db:
        assert (await db.get(RepositorySync, repo.id)).next_reconcile_at == due
        assert (
            await db.scalar(text("SELECT version_num FROM alembic_version"))
            == ScriptDirectory.from_config(migrated_database[1]).get_current_head()
        )


def test_sync_repair_upgrade_keeps_historical_success(empty_database):
    from datetime import UTC, datetime
    from uuid import uuid4

    from sqlalchemy import create_engine

    settings, config = empty_database
    command.upgrade(config, "0007_health_requests")
    engine = create_engine(settings.database_url.get_secret_value())
    repo, success = uuid4(), datetime(2026, 1, 1, tzinfo=UTC)
    try:
        with engine.begin() as db:
            db.execute(
                text(
                    "INSERT INTO github_installations (id,installation_id) VALUES (:id,11)"
                ),
                {"id": uuid4()},
            )
            db.execute(
                text(
                    "INSERT INTO repositories (id,github_repository_id,installation_id,full_name,private) VALUES (:id,101,11,'team/private',true)"
                ),
                {"id": repo},
            )
            db.execute(
                text(
                    "INSERT INTO repository_syncs (repository_id,last_reconciled_at,next_due_at) VALUES (:id,:success,:success)"
                ),
                {"id": repo, "success": success},
            )
        command.upgrade(config, "head")
        with engine.connect() as db:
            row = db.execute(
                text(
                    "SELECT last_reconciled_at,next_due_at,next_reconcile_at FROM repository_syncs WHERE repository_id=:id"
                ),
                {"id": repo},
            ).one()
            assert row == (success, success, None)
        # An unused deadline can be removed without discarding scheduling evidence.
        command.downgrade(config, "0007_health_requests")
        command.upgrade(config, "head")
    finally:
        engine.dispose()


def test_knowledge_migration_preserves_existing_data(empty_database):
    from uuid import uuid4

    from sqlalchemy import create_engine

    settings, config = empty_database
    command.upgrade(config, "0009_workspace")
    user_id, repo_id, job_id, version_id = (uuid4() for _ in range(4))
    engine = create_engine(settings.database_url.get_secret_value())
    try:
        with engine.begin() as db:
            db.execute(
                text("INSERT INTO users (id,github_id,role) VALUES (:id,42,'admin')"),
                {"id": user_id},
            )
            db.execute(
                text(
                    "INSERT INTO github_installations (id,installation_id) VALUES (:id,11)"
                ),
                {"id": uuid4()},
            )
            db.execute(
                text(
                    "INSERT INTO repositories (id,github_repository_id,installation_id,full_name,private) VALUES (:id,101,11,'team/private',true)"
                ),
                {"id": repo_id},
            )
            db.execute(
                text(
                    "INSERT INTO jobs (id,kind,repository_id,requested_by,input,input_hash,idempotency_key,status) VALUES (:id,'repository.workspace',:repo,NULL,'{}',:hash,'old','succeeded')"
                ),
                {"id": job_id, "repo": repo_id, "hash": "a" * 64},
            )
            db.execute(
                text(
                    "INSERT INTO workspace_versions (id,repository_id,source_repository_id,cache_repository_id,requested_by,idempotency_key,input_hash,selection,source,scopes,commit_sha,tree_oid,job_id,status) VALUES (:id,:repo,:repo,101,:user,'old',:hash,'{}','{}','[]',:sha,:sha,:job,'ready')"
                ),
                {
                    "id": version_id,
                    "repo": repo_id,
                    "user": user_id,
                    "job": job_id,
                    "hash": "a" * 64,
                    "sha": "b" * 40,
                },
            )
        command.upgrade(config, "head")
        with engine.begin() as db:
            assert (
                db.scalar(
                    text("SELECT github_id FROM users WHERE id=:id"), {"id": user_id}
                )
                == 42
            )
            assert (
                db.scalar(
                    text("SELECT full_name FROM repositories WHERE id=:id"),
                    {"id": repo_id},
                )
                == "team/private"
            )
            assert (
                db.scalar(text("SELECT status FROM jobs WHERE id=:id"), {"id": job_id})
                == "succeeded"
            )
            assert (
                db.scalar(
                    text("SELECT commit_sha FROM workspace_versions WHERE id=:id"),
                    {"id": version_id},
                )
                == "b" * 40
            )
            db.execute(
                text("INSERT INTO knowledge_corpora (repository_id) VALUES (:id)"),
                {"id": repo_id},
            )
        with pytest.raises(RuntimeError, match="Cannot discard knowledge provenance"):
            command.downgrade(config, "0009_workspace")
        with engine.connect() as db:
            assert db.scalar(text("SELECT count(*) FROM knowledge_corpora")) == 1
            assert (
                db.scalar(text("SELECT version_num FROM alembic_version"))
                == ScriptDirectory.from_config(config).get_current_head()
            )
    finally:
        engine.dispose()


def test_composed_migration_preserves_legacy_knowledge(empty_database):
    from datetime import UTC, datetime, timedelta
    from hashlib import sha256
    from uuid import uuid4

    from sqlalchemy import create_engine

    settings, config = empty_database
    command.upgrade(config, "0010_knowledge_sources")
    user, repo, job, imp, doc, revision, member = (uuid4() for _ in range(7))
    created = datetime(2026, 1, 1, tzinfo=UTC)
    body = b"legacy canonical source\n"
    digest = sha256(body).hexdigest()
    values = {
        "user": user,
        "repo": repo,
        "job": job,
        "imp": imp,
        "doc": doc,
        "revision": revision,
        "member": member,
        "created": created,
        "deadline": created + timedelta(days=1),
        "body": body,
        "size": len(body),
        "digest": digest,
    }
    engine = create_engine(settings.database_url.get_secret_value())
    try:
        with engine.begin() as db:
            db.execute(
                text("INSERT INTO users (id,github_id,role) VALUES (:user,42,'admin')"),
                values,
            )
            db.execute(
                text(
                    "INSERT INTO github_installations (id,installation_id) VALUES (:id,11)"
                ),
                {"id": uuid4()},
            )
            db.execute(
                text(
                    "INSERT INTO repositories (id,github_repository_id,installation_id,full_name,private) VALUES (:repo,101,11,'team/private',true)"
                ),
                values,
            )
            db.execute(
                text(
                    "INSERT INTO knowledge_corpora (repository_id,target_generation) VALUES (:repo,1)"
                ),
                values,
            )
            db.execute(
                text(
                    "INSERT INTO jobs (id,kind,repository_id,requested_by,input,input_hash,idempotency_key,status) VALUES (:job,'knowledge.import',:repo,:user,'{}',:digest,'legacy','queued')"
                ),
                values,
            )
            db.execute(
                text(
                    "INSERT INTO knowledge_imports (id,repository_id,source_repository_id,actor_id,actor_github_id,actor_auth_version,kind,idempotency_key,request_hash,request,target_generation,job_id,created_at,deadline) VALUES (:imp,:repo,:repo,:user,42,1,'upload','legacy',:digest,'{}',1,:job,:created,:deadline)"
                ),
                values,
            )
            db.execute(
                text(
                    "INSERT INTO knowledge_documents (id,repository_id,source_repository_id,kind) VALUES (:doc,:repo,:repo,'upload')"
                ),
                values,
            )
            db.execute(
                text(
                    "INSERT INTO knowledge_revisions (id,document_id,revision_hash,source_version,body,body_bytes) VALUES (:revision,:doc,:digest,:digest,:body,:size)"
                ),
                values,
            )
            db.execute(
                text(
                    "INSERT INTO knowledge_import_members (id,import_id,source_key,ordinal,revision_id) VALUES (:member,:imp,'legacy',0,:revision)"
                ),
                values,
            )
        command.upgrade(config, "head")
        command.check(config)
        with engine.begin() as db:
            assert db.execute(
                text(
                    "SELECT body,revision_hash,source_version FROM knowledge_revisions WHERE id=:revision"
                ),
                values,
            ).one() == (body, digest, digest)
            assert db.execute(
                text(
                    "SELECT composition_version,deadline FROM knowledge_imports WHERE id=:imp"
                ),
                values,
            ).one() == (0, values["deadline"])
            assert (
                db.scalar(
                    text(
                        "SELECT accepted_revision_id FROM knowledge_documents WHERE id=:doc"
                    ),
                    values,
                )
                is None
            )
            assert db.execute(
                text(
                    "SELECT member_kind,next_chunk,total_chunks,revision_id FROM knowledge_import_members WHERE id=:member"
                ),
                values,
            ).one() == ("upload", 0, None, revision)
            # New full manifests must never be silently downgraded to legacy deltas.
            db.execute(
                text(
                    "UPDATE knowledge_imports SET composition_version=1 WHERE id=:imp"
                ),
                values,
            )
        with pytest.raises(
            RuntimeError, match="Refusing to discard composed knowledge provenance"
        ):
            command.downgrade(config, "0010_knowledge_sources")
        with engine.connect() as db:
            assert (
                db.scalar(text("SELECT version_num FROM alembic_version"))
                == ScriptDirectory.from_config(config).get_current_head()
            )
            assert (
                db.scalar(
                    text("SELECT body FROM knowledge_revisions WHERE id=:revision"),
                    values,
                )
                == body
            )
    finally:
        engine.dispose()


def test_cleanup_kind_migration_preserves_jobs(empty_database):
    from uuid import uuid4

    from sqlalchemy import create_engine

    settings, config = empty_database
    command.upgrade(config, "0012_knowledge_stages")
    user, repo, import_job, workspace_job = (uuid4() for _ in range(4))
    engine = create_engine(settings.database_url.get_secret_value())
    try:
        with engine.begin() as db:
            db.execute(
                text("INSERT INTO users (id,github_id,role) VALUES (:id,42,'admin')"),
                {"id": user},
            )
            db.execute(
                text(
                    "INSERT INTO github_installations (id,installation_id) VALUES (:id,11)"
                ),
                {"id": uuid4()},
            )
            db.execute(
                text(
                    "INSERT INTO repositories (id,github_repository_id,installation_id,full_name,private) VALUES (:repo,101,11,'team/private',true)"
                ),
                {"repo": repo},
            )
            db.execute(
                text(
                    "INSERT INTO jobs (id,kind,repository_id,requested_by,input,input_hash,idempotency_key,status) VALUES (:id,'knowledge.import',:repo,:user,'{}',:hash,'import','queued')"
                ),
                {"id": import_job, "repo": repo, "user": user, "hash": "a" * 64},
            )
            db.execute(
                text(
                    "INSERT INTO jobs (id,kind,repository_id,requested_by,input,input_hash,idempotency_key,status) VALUES (:id,'repository.workspace',:repo,NULL,'{}',:hash,'workspace','succeeded')"
                ),
                {"id": workspace_job, "repo": repo, "hash": "b" * 64},
            )
        command.upgrade(config, "head")
        with engine.begin() as db:
            assert (
                db.scalar(
                    text(
                        "SELECT count(*) FROM jobs WHERE kind IN ('knowledge.import','repository.workspace')"
                    )
                )
                == 2
            )
            db.execute(
                text(
                    "INSERT INTO jobs (id,kind,repository_id,requested_by,input,input_hash,idempotency_key,status) VALUES (:id,'knowledge.cleanup',:repo,NULL,'{}',:hash,'cleanup','queued')"
                ),
                {"id": uuid4(), "repo": repo, "hash": "c" * 64},
            )
        with pytest.raises(RuntimeError, match="Cannot discard knowledge cleanup jobs"):
            command.downgrade(config, "0012_knowledge_stages")
        with engine.begin() as db:
            assert (
                db.scalar(text("SELECT version_num FROM alembic_version"))
                == ScriptDirectory.from_config(config).get_current_head()
            )
            assert (
                db.scalar(
                    text("SELECT count(*) FROM jobs WHERE kind='knowledge.cleanup'")
                )
                == 1
            )
            db.execute(text("DELETE FROM jobs WHERE kind='knowledge.cleanup'"))
        command.downgrade(config, "0012_knowledge_stages")
        with engine.connect() as db:
            assert (
                db.scalar(
                    text("SELECT count(*) FROM jobs WHERE kind='knowledge.cleanup'")
                )
                == 0
            )
        command.upgrade(config, "head")
    finally:
        engine.dispose()


def test_private_chat_downgrade_refuses_user_content(empty_database):
    from uuid import uuid4

    from sqlalchemy import create_engine

    settings, config = empty_database
    command.upgrade(config, "head")
    user, conversation = uuid4(), uuid4()
    engine = create_engine(settings.database_url.get_secret_value())
    try:
        with engine.begin() as db:
            db.execute(
                text("INSERT INTO users (id,github_id,role) VALUES (:id,42,'admin')"),
                {"id": user},
            )
            db.execute(
                text(
                    "INSERT INTO chat_conversations (id,actor_id,idempotency_key,input_hash) VALUES (:id,:user,'key',:hash)"
                ),
                {"id": conversation, "user": user, "hash": "a" * 64},
            )
        with pytest.raises(RuntimeError, match="Cannot discard private conversations"):
            command.downgrade(config, "0017_knowledge_queries")
        with engine.connect() as db:
            assert db.scalar(text("SELECT count(*) FROM chat_conversations")) == 1
            assert (
                db.scalar(text("SELECT version_num FROM alembic_version"))
                == ScriptDirectory.from_config(config).get_current_head()
            )
        with engine.begin() as db:
            db.execute(
                text("DELETE FROM chat_conversations WHERE id=:id"),
                {"id": conversation},
            )
        command.downgrade(config, "0017_knowledge_queries")
        with engine.connect() as db:
            assert db.scalar(text("SELECT to_regclass('chat_conversations')")) is None
        command.upgrade(config, "head")
    finally:
        engine.dispose()


def test_stage_downgrade_refuses_keyed_replay_loss(empty_database):
    from uuid import uuid4

    from sqlalchemy import create_engine

    settings, config = empty_database
    command.upgrade(config, "0012_knowledge_stages")
    user, repo, document, revision, stage = (uuid4() for _ in range(5))
    digest = "a" * 64
    engine = create_engine(settings.database_url.get_secret_value())
    try:
        with engine.begin() as db:
            db.execute(
                text("INSERT INTO users (id,github_id,role) VALUES (:id,42,'admin')"),
                {"id": user},
            )
            db.execute(
                text(
                    "INSERT INTO github_installations (id,installation_id) VALUES (:id,11)"
                ),
                {"id": uuid4()},
            )
            db.execute(
                text(
                    "INSERT INTO repositories (id,github_repository_id,installation_id,full_name,private) VALUES (:id,101,11,'team/private',true)"
                ),
                {"id": repo},
            )
            db.execute(
                text(
                    "INSERT INTO knowledge_documents (id,repository_id,source_repository_id,kind) VALUES (:id,:repo,:repo,'upload')"
                ),
                {"id": document, "repo": repo},
            )
            db.execute(
                text(
                    "INSERT INTO knowledge_revisions (id,document_id,revision_hash,source_version,body,body_bytes) VALUES (:id,:doc,:hash,:hash,:body,1)"
                ),
                {"id": revision, "doc": document, "hash": digest, "body": b"x"},
            )
            db.execute(
                text(
                    "INSERT INTO knowledge_upload_stages (id,actor_id,revision_id,filename,created_at,expires_at,repository_id,purpose,idempotency_key,request_hash,body_bytes) VALUES (:id,:user,:revision,'note.md',now(),now()+interval '1 day',:repo,'new','key',:hash,1)"
                ),
                {
                    "id": stage,
                    "user": user,
                    "revision": revision,
                    "repo": repo,
                    "hash": digest,
                },
            )
        with pytest.raises(
            RuntimeError, match="Cannot discard keyed upload stage replays"
        ):
            command.downgrade(config, "0011_knowledge_imports")
        with engine.connect() as db:
            assert (
                db.scalar(text("SELECT version_num FROM alembic_version"))
                == "0012_knowledge_stages"
            )
            assert (
                db.scalar(
                    text(
                        "SELECT idempotency_key FROM knowledge_upload_stages WHERE id=:id"
                    ),
                    {"id": stage},
                )
                == "key"
            )
        with engine.begin() as db:
            db.execute(
                text("DELETE FROM knowledge_upload_stages WHERE id=:id"),
                {"id": stage},
            )
        command.downgrade(config, "0011_knowledge_imports")
        with engine.connect() as db:
            assert (
                db.scalar(text("SELECT version_num FROM alembic_version"))
                == "0011_knowledge_imports"
            )
        command.upgrade(config, "head")
    finally:
        engine.dispose()
