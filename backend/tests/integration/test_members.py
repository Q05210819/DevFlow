import asyncio
from urllib.parse import parse_qs, urlsplit

import pytest
from sqlalchemy import func, select
from support.auth_app import auth_env as auth_env  # noqa: PLC0414
from support.auth_app import browser, login, member

from devflow.audit.models import AuditLog
from devflow.auth.models import Session, User
from devflow.errors import AppError
from devflow.github.models import GitHubCredential

pytestmark = pytest.mark.asyncio


async def test_last_admin_concurrent_changes(auth_env):
    first = await member(auth_env, role="admin")
    second = await member(auth_env, github_id=102, role="admin")
    one = await auth_env.service.resolve_session(
        (await login(auth_env)).raw_token.get_secret_value()
    )
    auth_env.github_id = 102
    two = await auth_env.service.resolve_session(
        (await login(auth_env)).raw_token.get_secret_value()
    )
    barrier = asyncio.Barrier(2)

    async def change(actor, target, **values):
        await barrier.wait()
        return await auth_env.service.update_member(actor, target, **values)

    results = await asyncio.gather(
        change(one, first.id, role="member", enabled=None),
        change(two, second.id, role=None, enabled=False),
        return_exceptions=True,
    )
    errors = [r for r in results if isinstance(r, AppError)]
    assert len(errors) == 1
    assert (errors[0].status, errors[0].code) == (409, "last_admin")
    async with auth_env.factory() as db:
        active_admin_count = await db.scalar(
            select(func.count())
            .select_from(User)
            .where(User.enabled, User.role == "admin")
        )
        assert active_admin_count >= 1
        assert (
            await db.scalar(
                select(AuditLog.reason_code).where(AuditLog.outcome == "failure")
            )
            == "last_admin"
        )


async def test_member_disabled_while_login_inflight(auth_env):
    target = await member(auth_env)
    await member(auth_env, github_id=102, role="admin")
    auth_env.github_id = 102
    admin = await auth_env.service.resolve_session(
        (await login(auth_env)).raw_token.get_secret_value()
    )
    auth_env.github_id = 101
    begun = await auth_env.service.begin_login("/")
    state = parse_qs(urlsplit(begun.authorize_url).query)["state"][0]
    entered, release = asyncio.Event(), asyncio.Event()

    async def block():
        entered.set()
        await release.wait()

    auth_env.before_user = block
    callback = asyncio.create_task(
        auth_env.service.complete_login(
            "code", state, begun.browser_nonce.get_secret_value()
        )
    )
    await entered.wait()
    try:
        await auth_env.service.update_member(admin, target.id, role=None, enabled=False)
        # Even immediate re-enable cannot let a pre-revocation login return.
        await auth_env.service.update_member(admin, target.id, role=None, enabled=True)
    finally:
        release.set()
    with pytest.raises(AppError) as error:
        await callback
    assert error.value.status == 403
    async with auth_env.factory() as db:
        assert (
            await db.scalar(
                select(func.count())
                .select_from(Session)
                .where(Session.user_id == target.id)
            )
            == 0
        )
        assert (
            await db.scalar(
                select(func.count())
                .select_from(GitHubCredential)
                .where(GitHubCredential.user_id == target.id)
            )
            == 0
        )


async def test_role_change_immediately_invalidates_sessions_and_stale_actor(auth_env):
    target = await member(auth_env, role="admin")
    await member(auth_env, github_id=102, role="admin")
    issue = await login(auth_env)
    old_actor = await auth_env.service.resolve_session(
        issue.raw_token.get_secret_value()
    )
    auth_env.github_id = 102
    admin = await auth_env.service.resolve_session(
        (await login(auth_env)).raw_token.get_secret_value()
    )
    await auth_env.service.update_member(admin, target.id, role="member", enabled=None)
    with pytest.raises(AppError) as error:
        await auth_env.service.resolve_session(issue.raw_token.get_secret_value())
    assert error.value.status == 401
    with pytest.raises(AppError):
        await auth_env.service.update_member(
            old_actor, target.id, role="admin", enabled=None
        )
    async with auth_env.factory() as db:
        row = await db.get(User, target.id)
        assert row.auth_version == 2 and row.auth_changed_at > target.auth_changed_at


async def test_member_routes_allowlist_conflict_pagination_and_audit(auth_env):
    await member(auth_env, role="admin")
    issue = await login(auth_env)
    async with browser(auth_env) as client:
        client.cookies.set("devflow_session", issue.raw_token.get_secret_value())
        csrf = (await client.get("/api/v1/session")).json()["csrf_token"]
        headers = {"Origin": "http://localhost:3000", "X-CSRF-Token": csrf}
        for github_id in (103, 104):
            response = await client.post(
                "/api/v1/admin/users", json={"github_id": github_id}, headers=headers
            )
            assert response.status_code == 201
        assert (
            await client.post(
                "/api/v1/admin/users", json={"github_id": 103}, headers=headers
            )
        ).status_code == 409
        first = (await client.get("/api/v1/admin/users?limit=2")).json()
        assert len(first["items"]) == 2 and first["next_cursor"]
        second = (
            await client.get(
                "/api/v1/admin/users",
                params={"limit": 2, "cursor": first["next_cursor"]},
            )
        ).json()
        assert len(second["items"]) == 1 and second["next_cursor"] is None
        assert not (
            {v["id"] for v in first["items"]} & {v["id"] for v in second["items"]}
        )
        assert (
            await client.get(
                "/api/v1/admin/users",
                params={"limit": 1, "cursor": first["next_cursor"]},
            )
        ).status_code == 400
        audit = (await client.get("/api/v1/admin/audit-logs?limit=2")).json()
        assert len(audit["items"]) == 2 and audit["next_cursor"]
        assert "synthetic" not in str(audit)
        assert (
            await client.get(
                "/api/v1/admin/audit-logs",
                params={"limit": 2, "cursor": first["next_cursor"]},
            )
        ).status_code == 400
        assert (await client.get("/api/v1/admin/users?limit=101")).status_code == 422
        target = first["items"][0]["id"]
        changed = await client.patch(
            "/api/v1/admin/users/" + target, json={"enabled": False}, headers=headers
        )
        assert changed.status_code == 200 and changed.json()["enabled"] is False


async def test_regular_member_cannot_manage_or_view_audit(auth_env):
    await member(auth_env)
    issue = await login(auth_env)
    async with browser(auth_env) as client:
        client.cookies.set("devflow_session", issue.raw_token.get_secret_value())
        for path in ("/api/v1/admin/users", "/api/v1/admin/audit-logs"):
            response = await client.get(path)
            assert response.status_code == 403


async def test_admin_role_denial_is_audited(auth_env):
    await member(auth_env)
    issue = await login(auth_env)
    async with browser(auth_env) as client:
        client.cookies.set("devflow_session", issue.raw_token.get_secret_value())
        assert (await client.get("/api/v1/admin/users")).status_code == 403
    async with auth_env.factory() as db:
        audit = await db.scalar(
            select(AuditLog).where(AuditLog.action == "permission.denied")
        )
        assert audit is not None
        assert (audit.outcome, audit.reason_code) == ("failure", "forbidden")


async def test_list_cursor_is_bound_to_admin_and_tied_timestamps(auth_env):
    from sqlalchemy import update

    first = await member(auth_env, role="admin")
    second = await member(auth_env, github_id=102, role="admin")
    await member(auth_env, github_id=103)
    one = await auth_env.service.resolve_session(
        (await login(auth_env)).raw_token.get_secret_value()
    )
    auth_env.github_id = 102
    two = await auth_env.service.resolve_session(
        (await login(auth_env)).raw_token.get_secret_value()
    )
    async with auth_env.factory.begin() as db:
        await db.execute(update(User).values(created_at=first.created_at))
    page = await auth_env.service.list_members(one, limit=2)
    with pytest.raises(AppError) as error:
        await auth_env.service.list_members(two, limit=2, cursor=page.next_cursor)
    assert error.value.code == "invalid_cursor"
    rest = await auth_env.service.list_members(one, limit=2, cursor=page.next_cursor)
    assert len(rest.items) == 1
    assert len({u.id for u in page.items + rest.items}) == 3
    assert {first.id, second.id}.issubset({u.id for u in page.items + rest.items})


async def test_disabling_member_clears_credentials_and_all_sessions(auth_env):
    target = await member(auth_env)
    first, second = await login(auth_env), await login(auth_env)
    await member(auth_env, github_id=102, role="admin")
    auth_env.github_id = 102
    admin = await auth_env.service.resolve_session(
        (await login(auth_env)).raw_token.get_secret_value()
    )
    await auth_env.service.update_member(admin, target.id, role=None, enabled=False)
    for issue in (first, second):
        with pytest.raises(AppError) as error:
            await auth_env.service.resolve_session(issue.raw_token.get_secret_value())
        assert error.value.status == 401
    async with auth_env.factory() as db:
        credential = await db.scalar(
            select(GitHubCredential).where(GitHubCredential.user_id == target.id)
        )
        assert credential.access_token_encrypted is None
        assert credential.refresh_token_encrypted is None
        assert credential.expires_at is None and credential.refresh_expires_at is None
        assert credential.needs_reauth and credential.version == 3
        user = await db.get(User, target.id)
        assert not user.enabled and user.auth_version == 2
        assert user.auth_changed_at > target.auth_changed_at


async def test_member_change_rolls_back_when_audit_write_fails(auth_env):
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError

    target = await member(auth_env)
    issue = await login(auth_env)
    await member(auth_env, github_id=102, role="admin")
    auth_env.github_id = 102
    admin = await auth_env.service.resolve_session(
        (await login(auth_env)).raw_token.get_secret_value()
    )
    async with auth_env.factory.begin() as db:
        await db.execute(
            text(
                "CREATE FUNCTION reject_member_audit() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF NEW.action = 'member.updated' THEN RAISE EXCEPTION 'test audit failure'; END IF; RETURN NEW; END $$"
            )
        )
        await db.execute(
            text(
                "CREATE TRIGGER reject_member_audit BEFORE INSERT ON audit_logs FOR EACH ROW EXECUTE FUNCTION reject_member_audit()"
            )
        )
    with pytest.raises(DBAPIError):
        await auth_env.service.update_member(admin, target.id, role=None, enabled=False)
    assert (
        await auth_env.service.resolve_session(issue.raw_token.get_secret_value())
    ).id == target.id
    async with auth_env.factory() as db:
        user = await db.get(User, target.id)
        credential = await db.scalar(
            select(GitHubCredential).where(GitHubCredential.user_id == target.id)
        )
        assert (
            user.enabled
            and user.auth_version == 1
            and user.auth_changed_at == target.auth_changed_at
        )
        assert credential.version == 1 and not credential.needs_reauth
        assert (
            credential.access_token_encrypted is not None
            and credential.refresh_token_encrypted is not None
        )
