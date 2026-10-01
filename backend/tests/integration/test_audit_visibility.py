import pytest
from support.auth_app import auth_env as auth_env  # noqa: PLC0414
from support.auth_app import browser, login, member

pytestmark = pytest.mark.asyncio


async def test_audit_visibility_requires_admin_and_returns_redacted_pages(auth_env):
    await member(auth_env, role="admin")
    await member(auth_env, github_id=102)
    admin_issue = await login(auth_env)
    auth_env.github_id = 102
    member_issue = await login(auth_env)
    async with browser(auth_env) as client:
        assert (await client.get("/api/v1/admin/audit-logs")).status_code == 401
        client.cookies.set("devflow_session", member_issue.raw_token.get_secret_value())
        response = await client.get("/api/v1/admin/audit-logs")
        assert response.status_code == 403
        assert set(response.json()["error"]) == {"code", "message", "request_id"}
        client.cookies.set("devflow_session", admin_issue.raw_token.get_secret_value())
        first = await client.get("/api/v1/admin/audit-logs", params={"limit": 2})
        assert first.status_code == 200
        page = first.json()
        assert len(page["items"]) == 2 and page["next_cursor"]
        for row in page["items"]:
            assert set(row) == {
                "id",
                "actor_id",
                "action",
                "resource_type",
                "resource_id",
                "outcome",
                "request_id",
                "job_id",
                "reason_code",
                "created_at",
            }
        assert "synthetic" not in first.text
        second = await client.get(
            "/api/v1/admin/audit-logs",
            params={"limit": 2, "cursor": page["next_cursor"]},
        )
        assert second.status_code == 200
        assert len(second.json()["items"]) == 1
        assert second.json()["next_cursor"] is None
        assert not (
            {r["id"] for r in page["items"]} & {r["id"] for r in second.json()["items"]}
        )
        invalid = await client.get(
            "/api/v1/admin/audit-logs", params={"cursor": "invalid"}
        )
        assert invalid.status_code == 400
