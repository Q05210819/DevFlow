import asyncio
import hashlib
import hmac
import json
from datetime import timedelta
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import func, select, text, update
from support.auth_app import auth_env as auth_env  # noqa: PLC0414
from support.auth_app import login, member

from devflow.audit.models import AuditLog
from devflow.auth.models import Session, User
from devflow.errors import AppError
from devflow.github.models import GitHubCredential

pytestmark = pytest.mark.asyncio
SECRET = b"synthetic-webhook-secret"


def signed(body):
    return "sha256=" + hmac.new(SECRET, body, hashlib.sha256).hexdigest()


def payload(action="deleted", **fields):
    return json.dumps({"action": action, "installation": {"id": 10}, **fields}).encode()


@pytest.mark.parametrize("signature", ["", "sha1=bad", "sha256=" + "0" * 64, "ü"])
async def test_invalid_signature_before_json_and_safe_audit(db_factory, signature):
    from devflow.github.webhooks import WebhookService

    body = b"malformed-json-secret-sentinel"
    service = WebhookService(db_factory, SECRET)
    with pytest.raises(AppError) as caught:
        await service.handle_webhook(body, signature, str(uuid4()), "installation")
    assert caught.value.status == 401
    async with db_factory() as db:
        audits = list(await db.scalars(select(AuditLog)))
    assert len(audits) == 1
    assert audits[0].action == "webhook.verified"
    assert audits[0].outcome == "failure"
    assert audits[0].reason_code == "invalid_signature"
    assert "sentinel" not in repr(vars(audits[0]))


async def test_signature_covers_exact_bytes(db_factory):
    from devflow.github.webhooks import WebhookService

    service = WebhookService(db_factory, SECRET)
    original = payload()
    with pytest.raises(AppError) as caught:
        await service.handle_webhook(
            original + b" ", signed(original), str(uuid4()), "installation"
        )
    assert caught.value.status == 401


async def test_signed_malformed_json_is_validation_error(db_factory):
    from devflow.github.webhooks import WebhookService

    body = b"{"
    with pytest.raises(AppError) as caught:
        await WebhookService(db_factory, SECRET).handle_webhook(
            body, signed(body), str(uuid4()), "installation"
        )
    assert caught.value.status == 400


@pytest.mark.parametrize(
    "fields",
    [
        {"installation": {"id": True}},
        {"installation": {"id": -1}},
        {"installation": {"id": "10"}},
        {"installation": {"id": 2**63}},
        {"installation": None},
    ],
)
async def test_signed_known_event_requires_numeric_identity(db_factory, fields):
    from devflow.github.webhooks import WebhookService

    body = payload(**fields)
    with pytest.raises(AppError) as caught:
        await WebhookService(db_factory, SECRET).handle_webhook(
            body, signed(body), str(uuid4()), "installation"
        )
    assert caught.value.status == 400


async def test_signed_revocation_requires_sender_id(db_factory):
    from devflow.github.webhooks import WebhookService

    body = json.dumps({"action": "revoked", "sender": {"login": "not-an-id"}}).encode()
    with pytest.raises(AppError) as caught:
        await WebhookService(db_factory, SECRET).handle_webhook(
            body, signed(body), str(uuid4()), "github_app_authorization"
        )
    assert caught.value.status == 400


async def deliver(factory, body=None, event="installation", delivery_id=None):
    from devflow.github.webhooks import WebhookService

    body = payload() if body is None else body
    await WebhookService(factory, SECRET).handle_webhook(
        body, signed(body), delivery_id or str(uuid4()), event
    )


async def seed_repository(factory):
    from devflow.github.models import GitHubInstallation
    from devflow.repositories.models import Repository

    async with factory.begin() as db:
        installation = GitHubInstallation(
            installation_id=10, available=True, needs_verification=False
        )
        db.add(installation)
        await db.flush()
        repository = Repository(
            github_repository_id=100,
            installation_id=10,
            full_name="org/private",
            private=True,
            available=True,
            needs_verification=False,
        )
        db.add(repository)
        await db.flush()
        return repository.id


async def lifecycle_snapshot(factory):
    from devflow.github.models import GitHubInstallation, WebhookDelivery
    from devflow.repositories.models import Repository

    async with factory() as db:
        return (
            await db.scalar(select(GitHubInstallation)),
            await db.scalar(select(Repository)),
            list(await db.scalars(select(WebhookDelivery))),
            list(await db.scalars(select(AuditLog))),
        )


async def test_bad_signature_does_not_reserve_delivery(db_factory):
    from devflow.github.models import WebhookDelivery
    from devflow.github.webhooks import WebhookService

    await seed_repository(db_factory)
    delivery_id = str(uuid4())
    with pytest.raises(AppError):
        await WebhookService(db_factory, SECRET).handle_webhook(
            payload(), "bad", delivery_id, "installation"
        )
    async with db_factory() as db:
        assert await db.scalar(select(func.count()).select_from(WebhookDelivery)) == 0
    await deliver(db_factory, delivery_id=delivery_id)
    installation, repository, deliveries, _ = await lifecycle_snapshot(db_factory)
    assert len(deliveries) == 1
    assert not installation.available and not repository.available
    # An existing delivery ID must not turn an unauthenticated request into204.
    with pytest.raises(AppError) as caught:
        await WebhookService(db_factory, SECRET).handle_webhook(
            b"not-json", "bad", delivery_id, "installation"
        )
    assert caught.value.status == 401
    installation, repository, deliveries, _ = await lifecycle_snapshot(db_factory)
    assert len(deliveries) == 1
    assert installation.auth_version == repository.auth_version == 2


async def test_duplicate_delivery_is_atomic(db_factory, migrated_database):
    from devflow.db import create_session_factory

    await seed_repository(db_factory)
    other = create_session_factory(migrated_database[0])
    delivery_id = str(uuid4())
    barrier = asyncio.Barrier(2)

    async def run(factory):
        await barrier.wait()
        await deliver(factory, delivery_id=delivery_id)

    try:
        await asyncio.gather(run(db_factory), run(other))
    finally:
        await other.kw["bind"].dispose()
    installation, repository, deliveries, audits = await lifecycle_snapshot(db_factory)
    assert len(deliveries) == 1
    assert deliveries[0].status == "processed"
    assert installation.auth_version == repository.auth_version == 2
    assert sum(a.action == "webhook.processed" for a in audits) == 1
    assert sum(a.action == "repository.invalidated" for a in audits) == 1


@pytest.mark.parametrize("action", ["deleted", "suspend"])
async def test_uninstall_invalidates_installation_and_repositories(db_factory, action):
    await seed_repository(db_factory)
    await deliver(db_factory, payload(action))
    installation, repository, _, _ = await lifecycle_snapshot(db_factory)
    for row in (installation, repository):
        assert not row.available and not row.needs_verification
        assert row.auth_version == 2


@pytest.mark.parametrize(
    "action,repository_id",
    [("removed", 100), ("removed", 999), ("added", 100), ("added", 999)],
)
async def test_every_repository_coverage_change_advances_installation_epoch(
    db_factory, action, repository_id
):
    await seed_repository(db_factory)
    await deliver(
        db_factory,
        payload(action, **{"repositories_" + action: [{"id": repository_id}]}),
        "installation_repositories",
    )
    installation, repository, _, _ = await lifecycle_snapshot(db_factory)
    assert installation.auth_version == 2
    assert repository.auth_version == (2 if repository_id == 100 else 1)
    if repository_id == 100:
        assert repository.needs_verification == (action == "added")
        assert repository.available == (action == "added")


async def test_unknown_installation_deletion_persists_tombstone(db_factory):
    await deliver(db_factory)
    installation, repository, _, _ = await lifecycle_snapshot(db_factory)
    assert repository is None
    assert installation.installation_id == 10
    assert not installation.available and not installation.needs_verification


@pytest.mark.parametrize("action", ["created", "unsuspend", "new_permissions_accepted"])
async def test_old_restore_requires_live_verification(db_factory, action):
    await seed_repository(db_factory)
    await deliver(db_factory)
    await deliver(db_factory, payload(action))
    installation, repository, _, _ = await lifecycle_snapshot(db_factory)
    for row in (installation, repository):
        assert not row.available and row.needs_verification
        assert row.auth_version == 3


async def test_empty_removed_list_requires_reverification(db_factory):
    await seed_repository(db_factory)
    await deliver(
        db_factory,
        payload("removed", repositories_removed=[], repository_selection="selected"),
        "installation_repositories",
    )
    installation, repository, _, _ = await lifecycle_snapshot(db_factory)
    assert installation.auth_version == 2
    assert repository.needs_verification
    assert repository.auth_version == 2


async def test_unknown_valid_event_is_ignored_without_payload_storage(db_factory):
    body = json.dumps(
        {"secret": "raw-payload-sentinel", "action": "arbitrary-secret"}
    ).encode()
    await deliver(db_factory, body, "unrecognized-secret-event")
    _, _, deliveries, audits = await lifecycle_snapshot(db_factory)
    assert len(deliveries) == 1 and deliveries[0].status == "ignored"
    assert deliveries[0].event_name == "unknown" and deliveries[0].action is None
    assert "sentinel" not in repr(vars(deliveries[0]))
    assert len(audits) == 1 and audits[0].action == "webhook.processed"


async def test_required_audit_failure_rolls_back_receipt_and_revocation(db_factory):
    await seed_repository(db_factory)
    async with db_factory.begin() as db:
        await db.execute(
            text(
                "CREATE FUNCTION reject_webhook_audit() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF NEW.action = 'webhook.processed' THEN RAISE EXCEPTION 'synthetic audit failure'; END IF; RETURN NEW; END $$"
            )
        )
        await db.execute(
            text(
                "CREATE TRIGGER reject_webhook_audit BEFORE INSERT ON audit_logs FOR EACH ROW EXECUTE FUNCTION reject_webhook_audit()"
            )
        )
    delivery_id = str(uuid4())
    with pytest.raises(AppError) as caught:
        await deliver(db_factory, delivery_id=delivery_id)
    assert caught.value.status == 503
    installation, repository, deliveries, audits = await lifecycle_snapshot(db_factory)
    assert installation.available and repository.available
    assert installation.auth_version == repository.auth_version == 1
    assert deliveries == audits == []
    async with db_factory.begin() as db:
        await db.execute(text("DROP TRIGGER reject_webhook_audit ON audit_logs"))
    await deliver(db_factory, delivery_id=delivery_id)
    assert len((await lifecycle_snapshot(db_factory))[2]) == 1


def user_revoked(github_id=101):
    return json.dumps(
        {
            "action": "revoked",
            "sender": {"id": github_id, "login": "untrusted-display-name"},
        }
    ).encode()


async def test_user_revocation_clears_tokens_sessions_and_advances_cutoff(auth_env):
    user = await member(auth_env)
    issue = await login(auth_env)
    await deliver(auth_env.factory, user_revoked(), "github_app_authorization")
    with pytest.raises(AppError) as caught:
        await auth_env.service.resolve_session(issue.raw_token.get_secret_value())
    assert caught.value.status == 401
    async with auth_env.factory() as db:
        current = await db.get(User, user.id)
        credential = await db.scalar(select(GitHubCredential))
        assert current.auth_version == 2
        assert current.auth_changed_order > user.auth_changed_order
        assert current.enabled and current.role == "member"
        assert credential.needs_reauth and credential.version == 2
        assert (
            credential.access_token_encrypted
            is credential.refresh_token_encrypted
            is None
        )
        assert credential.expires_at is credential.refresh_expires_at is None
        assert await db.scalar(select(func.count()).select_from(Session)) == 0
        assert (
            await db.scalar(
                select(func.count())
                .select_from(AuditLog)
                .where(AuditLog.action == "credential.revoked")
            )
            == 1
        )


@pytest.mark.parametrize("inflight", [False, True])
async def test_user_revocation_cannot_be_undone_by_late_login(auth_env, inflight):
    await member(auth_env)
    start = await auth_env.service.begin_login("/repositories")
    state = parse_qs(urlsplit(start.authorize_url).query)["state"][0]
    entered, release = asyncio.Event(), asyncio.Event()

    async def pause_user():
        entered.set()
        await release.wait()

    if inflight:
        auth_env.before_user = pause_user
        pending = asyncio.create_task(
            auth_env.service.complete_login(
                "code", state, start.browser_nonce.get_secret_value()
            )
        )
        await asyncio.wait_for(entered.wait(), 5)
    await deliver(auth_env.factory, user_revoked(), "github_app_authorization")
    if inflight:
        release.set()
    else:
        pending = asyncio.create_task(
            auth_env.service.complete_login(
                "code", state, start.browser_nonce.get_secret_value()
            )
        )
    with pytest.raises(AppError) as caught:
        await pending
    assert caught.value.status == 403
    async with auth_env.factory() as db:
        assert await db.scalar(select(func.count()).select_from(Session)) == 0
        assert await db.scalar(select(func.count()).select_from(GitHubCredential)) == 0
        assert (await db.scalar(select(User))).auth_version == 2


async def test_unknown_user_revocation_does_not_create_member_or_allow_old_login(
    auth_env,
):
    start = await auth_env.service.begin_login("/repositories")
    state = parse_qs(urlsplit(start.authorize_url).query)["state"][0]
    await deliver(auth_env.factory, user_revoked(), "github_app_authorization")
    async with auth_env.factory() as db:
        assert await db.scalar(select(func.count()).select_from(User)) == 0
    await member(auth_env)
    with pytest.raises(AppError) as caught:
        await auth_env.service.complete_login(
            "code", state, start.browser_nonce.get_secret_value()
        )
    assert caught.value.status == 403


async def test_user_revocation_cannot_be_undone_by_late_refresh(auth_env):
    from devflow.github.client import GitHubClient
    from devflow.github.credentials import CredentialService

    await member(auth_env)
    issue = await login(auth_env)
    actor = await auth_env.service.resolve_session(issue.raw_token.get_secret_value())
    async with auth_env.factory.begin() as db:
        await db.execute(
            update(GitHubCredential).values(
                expires_at=func.clock_timestamp() - timedelta(minutes=1)
            )
        )
    entered, release = asyncio.Event(), asyncio.Event()

    async def remote(request):
        entered.set()
        await release.wait()
        return httpx.Response(
            200,
            json={
                "access_token": "new-access",
                "refresh_token": "new-refresh",
                "expires_in": 3600,
                "refresh_token_expires_in": 7200,
                "token_type": "bearer",
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(remote)) as http:
        service = CredentialService(
            auth_env.factory, GitHubClient(http, auth_env.settings), auth_env.cipher
        )
        refreshing = asyncio.create_task(service.get_user_token(actor))
        await asyncio.wait_for(entered.wait(), 5)
        revoking = asyncio.create_task(
            deliver(auth_env.factory, user_revoked(), "github_app_authorization")
        )
        try:
            async with asyncio.timeout(5), auth_env.factory() as observer:
                while not await observer.scalar(
                    text(
                        "SELECT EXISTS (SELECT 1 FROM pg_locks waiting JOIN pg_locks holding ON holding.pid = ANY(pg_blocking_pids(waiting.pid)) WHERE NOT waiting.granted AND holding.relation = 'users'::regclass)"
                    )
                ):
                    if revoking.done():
                        await revoking
                        pytest.fail(
                            "Revocation did not serialize on the refreshing user"
                        )
                    await asyncio.sleep(0)
        finally:
            release.set()
            await asyncio.gather(refreshing, revoking)
        with pytest.raises(AppError) as caught:
            await service.get_user_token(actor)
        assert caught.value.status == 401
    async with auth_env.factory() as db:
        credential = await db.scalar(select(GitHubCredential))
        assert credential.version == 3 and credential.needs_reauth
        assert (
            credential.access_token_encrypted
            is credential.refresh_token_encrypted
            is None
        )
        assert (await db.scalar(select(User))).auth_version == 2


async def test_signed_webhook_uses_real_factory_without_cookie_or_csrf(
    migrated_database, tmp_path
):
    from devflow.app import create_app

    secret_file = tmp_path / "webhook-secret"
    secret_file.write_bytes(SECRET)
    settings = migrated_database[0].model_copy(
        update={"github_webhook_secret_file": secret_file}
    )
    app = create_app(settings)
    body = payload()
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost:3000"
        ) as client,
    ):
        headers = {
            "X-Hub-Signature-256": signed(body),
            "X-GitHub-Delivery": str(uuid4()),
            "X-GitHub-Event": "installation",
        }
        invalid = await client.post(
            "/api/v1/github/webhooks",
            content=b"malformed secret sentinel",
            headers={**headers, "X-Hub-Signature-256": "bad"},
        )
        assert invalid.status_code == 401
        response = await client.post(
            "/api/v1/github/webhooks",
            content=body,
            headers=headers,
        )
        duplicate = await client.post(
            "/api/v1/github/webhooks", content=body, headers=headers
        )
        assert duplicate.status_code == 204
    assert response.status_code == 204 and response.content == b""


@pytest_asyncio.fixture
async def policy_env(auth_env, tmp_path):
    from devflow.db import create_session_factory
    from devflow.github.client import GitHubClient
    from devflow.github.credentials import CredentialService
    from devflow.repositories.policy import RepositoryPolicy
    from devflow.repositories.service import RepositoryService

    await member(auth_env, role="admin")
    issue = await login(auth_env)
    actor = await auth_env.service.resolve_session(issue.raw_token.get_secret_value())
    key_file = tmp_path / "app-private-key"
    key_file.write_bytes(
        rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    settings = auth_env.settings.model_copy(
        update={
            "github_app_id": 123,
            "github_allowed_installation_ids": [10],
            "github_private_key_file": key_file,
        }
    )
    env = SimpleNamespace(
        factory=auth_env.factory, actor=actor, before_repositories=None, visible=True
    )
    installation = {"id": 10, "app_id": 123, "account": {"id": 7}, "suspended_at": None}
    repository = {
        "id": 100,
        "full_name": "org/private",
        "private": True,
        "default_branch": "main",
    }

    async def remote(request):
        if request.url.path == "/user/installations":
            return httpx.Response(200, json={"installations": [installation]})
        if request.url.path == "/app/installations/10":
            return httpx.Response(200, json=installation)
        if request.url.path == "/user/installations/10/repositories":
            if env.before_repositories:
                await env.before_repositories()
            return httpx.Response(
                200, json={"repositories": [repository] if env.visible else []}
            )
        raise AssertionError("Unexpected external endpoint")

    other = create_session_factory(settings)
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(remote)) as http:
            github = GitHubClient(http, settings)
            env.policies = [
                RepositoryPolicy(
                    factory,
                    github,
                    CredentialService(factory, github, auth_env.cipher),
                    settings,
                )
                for factory in (auth_env.factory, other)
            ]
            env.service = RepositoryService(
                auth_env.factory,
                github,
                CredentialService(auth_env.factory, github, auth_env.cipher),
                settings,
                policy=env.policies[0],
            )
            env.repository = await env.service.register(actor, 10, 100)
            yield env
    finally:
        await other.kw["bind"].dispose()


@pytest.mark.parametrize(
    "event,body",
    [
        ("installation", payload("deleted")),
        ("installation", payload("suspend")),
        (
            "installation_repositories",
            payload("removed", repositories_removed=[{"id": 100}]),
        ),
        (
            "installation_repositories",
            payload("removed", repositories_removed=[{"id": 999}]),
        ),
        (
            "installation_repositories",
            payload("added", repositories_added=[{"id": 999}]),
        ),
    ],
)
async def test_uninstall_or_repository_remove_invalidates_access_in_independent_caches(
    policy_env, event, body
):
    env = policy_env
    for policy in env.policies:
        assert (
            await policy.require(env.actor, env.repository.id)
        ).id == env.repository.id
    await deliver(env.factory, body, event)
    env.visible = False
    for policy in env.policies:
        with pytest.raises(AppError) as caught:
            await policy.require(env.actor, env.repository.id)
        assert caught.value.status == 404


async def test_old_live_restoration_cannot_override_newer_remove(policy_env):
    env = policy_env
    await deliver(env.factory)
    await deliver(env.factory, payload("created"))
    entered, release = asyncio.Event(), asyncio.Event()

    async def pause():
        entered.set()
        await release.wait()

    env.before_repositories = pause
    restoring = asyncio.create_task(env.service.register(env.actor, 10, 100))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        await deliver(
            env.factory,
            payload("removed", repositories_removed=[{"id": 100}]),
            "installation_repositories",
        )
    finally:
        release.set()
    with pytest.raises(AppError) as caught:
        await restoring
    assert caught.value.status == 404
    installation, repository, _, _ = await lifecycle_snapshot(env.factory)
    assert not repository.available and not repository.needs_verification
    assert not installation.available


async def test_inflight_policy_read_cannot_promote_restore_marker(policy_env):
    env = policy_env
    entered, release = asyncio.Event(), asyncio.Event()

    async def pause():
        entered.set()
        await release.wait()

    env.before_repositories = pause
    reading = asyncio.create_task(
        env.policies[0].require(env.actor, env.repository.id, fresh=True)
    )
    try:
        await asyncio.wait_for(entered.wait(), 5)
        await deliver(env.factory, payload("unsuspend"))
    finally:
        release.set()
    with pytest.raises(AppError) as caught:
        await reading
    assert caught.value.status == 404
    installation, repository, _, _ = await lifecycle_snapshot(env.factory)
    assert installation.needs_verification and repository.needs_verification


async def test_restore_requires_explicit_verified_registration(policy_env):
    env = policy_env
    await deliver(env.factory)
    await deliver(env.factory, payload("unsuspend"))
    for policy in env.policies:
        with pytest.raises(AppError):
            await policy.require(env.actor, env.repository.id)
    restored = await env.service.register(env.actor, 10, 100)
    assert restored.id == env.repository.id
    for policy in env.policies:
        assert (await policy.require(env.actor, env.repository.id)).id == restored.id


async def test_database_error_returns_503_and_retry_is_not_reserved(
    db_factory, migrated_database, tmp_path
):
    from devflow.app import create_app

    secret_file = tmp_path / "webhook-secret"
    secret_file.write_bytes(SECRET)
    app = create_app(
        migrated_database[0].model_copy(
            update={"github_webhook_secret_file": secret_file}
        )
    )
    body = payload()
    headers = {
        "X-Hub-Signature-256": signed(body),
        "X-GitHub-Delivery": str(uuid4()),
        "X-GitHub-Event": "installation",
    }
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost:3000"
        ) as client,
    ):
        async with db_factory.begin() as db:
            await db.execute(
                text(
                    "ALTER TABLE webhook_deliveries RENAME TO temporarily_unavailable_deliveries"
                )
            )
        try:
            response = await client.post(
                "/api/v1/github/webhooks", content=body, headers=headers
            )
            assert response.status_code == 503
        finally:
            async with db_factory.begin() as db:
                await db.execute(
                    text(
                        "ALTER TABLE temporarily_unavailable_deliveries RENAME TO webhook_deliveries"
                    )
                )
        assert (
            await client.post("/api/v1/github/webhooks", content=body, headers=headers)
        ).status_code == 204
    assert len((await lifecycle_snapshot(db_factory))[2]) == 1


@pytest.mark.parametrize("failing_action", ["credential.revoked", "webhook.processed"])
async def test_user_revocation_audit_failure_restores_credentials_and_sessions(
    auth_env, failing_action
):
    from devflow.github.models import WebhookDelivery

    await member(auth_env)
    issue = await login(auth_env)
    async with auth_env.factory.begin() as db:
        # Values are a fixed local test parametrization, never request input.
        await db.execute(
            text(
                "CREATE FUNCTION fail_revocation() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF NEW.action = '"
                + failing_action
                + "' THEN RAISE EXCEPTION 'synthetic audit failure'; END IF; RETURN NEW; END $$"
            )
        )
        await db.execute(
            text(
                "CREATE TRIGGER fail_revocation BEFORE INSERT ON audit_logs FOR EACH ROW EXECUTE FUNCTION fail_revocation()"
            )
        )
    with pytest.raises(AppError) as caught:
        await deliver(auth_env.factory, user_revoked(), "github_app_authorization")
    assert caught.value.status == 503
    actor = await auth_env.service.resolve_session(issue.raw_token.get_secret_value())
    assert actor.auth_version == 1
    async with auth_env.factory() as db:
        credential = await db.scalar(select(GitHubCredential))
        assert credential.version == 1 and not credential.needs_reauth
        assert (
            auth_env.cipher.decrypt(
                credential.access_token_encrypted
            ).get_secret_value()
            == "synthetic-access"
        )
        assert (
            auth_env.cipher.decrypt(
                credential.refresh_token_encrypted
            ).get_secret_value()
            == "synthetic-refresh"
        )
        assert await db.scalar(select(func.count()).select_from(WebhookDelivery)) == 0


async def test_refresh_waiting_behind_revocation_cannot_restore_tokens(auth_env):
    from devflow.github.client import GitHubClient
    from devflow.github.credentials import CredentialService

    await member(auth_env)
    issue = await login(auth_env)
    actor = await auth_env.service.resolve_session(issue.raw_token.get_secret_value())
    async with auth_env.factory.begin() as db:
        await db.execute(
            update(GitHubCredential).values(
                expires_at=func.clock_timestamp() - timedelta(minutes=1)
            )
        )
    gate = uuid4().int % (2**62)
    async with auth_env.factory.begin() as db:
        await db.execute(
            text(
                "CREATE FUNCTION hold_user_revocation() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF NEW.auth_version > OLD.auth_version THEN PERFORM pg_advisory_xact_lock("
                + str(gate)
                + "); END IF; RETURN NEW; END $$"
            )
        )
        await db.execute(
            text(
                "CREATE TRIGGER hold_user_revocation BEFORE UPDATE ON users FOR EACH ROW EXECUTE FUNCTION hold_user_revocation()"
            )
        )

    def unexpected_refresh(request):
        raise AssertionError("Processed revocation must prevent provider refresh")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(unexpected_refresh)
    ) as http:
        service = CredentialService(
            auth_env.factory, GitHubClient(http, auth_env.settings), auth_env.cipher
        )
        async with auth_env.factory.begin() as blocker:
            blocker_pid = await blocker.scalar(select(func.pg_backend_pid()))
            await blocker.execute(select(func.pg_advisory_xact_lock(gate)))
            revoking = asyncio.create_task(
                deliver(auth_env.factory, user_revoked(), "github_app_authorization")
            )
            async with asyncio.timeout(5), auth_env.factory() as observer:
                while not (
                    revoker_pid := await observer.scalar(
                        text(
                            "SELECT pid FROM pg_locks WHERE NOT granted AND :blocker = ANY(pg_blocking_pids(pid)) LIMIT 1"
                        ),
                        {"blocker": blocker_pid},
                    )
                ):
                    if revoking.done():
                        await revoking
                        pytest.fail("Expected revocation to hold the user lock")
                    await asyncio.sleep(0)
            refreshing = asyncio.create_task(service.get_user_token(actor))
            async with asyncio.timeout(5), auth_env.factory() as observer:
                while not await observer.scalar(
                    text(
                        "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE NOT granted AND :blocker = ANY(pg_blocking_pids(pid)))"
                    ),
                    {"blocker": revoker_pid},
                ):
                    if refreshing.done():
                        await refreshing
                        pytest.fail("Refresh bypassed the revocation lock")
                    await asyncio.sleep(0)
        await revoking
        with pytest.raises(AppError) as caught:
            await refreshing
        assert caught.value.status == 401
    async with auth_env.factory() as db:
        credential = await db.scalar(select(GitHubCredential))
        assert credential.version == 2 and credential.needs_reauth
        assert (
            credential.access_token_encrypted
            is credential.refresh_token_encrypted
            is None
        )
