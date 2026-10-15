import pytest
from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy import text


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
            == "0006_content_webhooks"
        )
        table = {
            "content_receipt": "webhook_deliveries",
            "command_receipt": "repository_command_receipts",
            "version_hint": "repository_syncs",
        }[evidence]
        assert await db.scalar(text(f"SELECT count(*) FROM {table}")) == 1
