import pytest
from pydantic import ValidationError
from sqlalchemy import select


@pytest.mark.asyncio
async def test_audit_rolls_back_with_mutation(db_factory):
    from devflow.audit.models import AuditLog
    from devflow.audit.service import AuditEntry, write_audit
    from devflow.auth.models import User

    with pytest.raises(RuntimeError, match="abort"):
        async with db_factory.begin() as session:
            user = User(github_id=42, role="member")
            session.add(user)
            await session.flush()
            audit_id = await write_audit(
                session,
                AuditEntry(
                    actor_id=user.id,
                    action="member.created",
                    resource_type="user",
                    resource_id=user.id,
                    outcome="success",
                ),
            )
            assert await session.get(AuditLog, audit_id) is not None
            # Another connection cannot see an uncommitted audit or user.
            async with db_factory() as other:
                assert list(await other.scalars(select(AuditLog))) == []
                assert list(await other.scalars(select(User))) == []
            raise RuntimeError("abort")
    async with db_factory() as session:
        new_users = list(await session.scalars(select(User)))
        new_audits = list(await session.scalars(select(AuditLog)))
        assert new_users == [] and new_audits == []
    async with db_factory.begin() as session:
        await write_audit(
            session,
            AuditEntry(
                action="auth.login",
                resource_type="session",
                outcome="failure",
                reason_code="unauthenticated",
            ),
        )
    async with db_factory() as session:
        row = (await session.scalars(select(AuditLog))).one()
        assert row.actor_id is None and row.reason_code == "unauthenticated"
        assert row.created_at.utcoffset().total_seconds() == 0


@pytest.mark.parametrize(
    "extra",
    [
        {"raw_response": {"token": "secret"}},
        {"reason_code": "token-secret"},
        {"outcome": "denied"},
        {"action": "token-secret"},
        {"outcome": "failure", "reason_code": None},
    ],
)
def test_audit_rejects_uncontrolled_fields(extra):
    from devflow.audit.service import AuditEntry

    values = {"action": "auth.login", "resource_type": "session", "outcome": "success"}
    values.update(extra)
    with pytest.raises(ValidationError):
        AuditEntry(**values)
