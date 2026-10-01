"""Explicit first-database administrator bootstrap: python -m devflow.bootstrap."""

import asyncio

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from devflow.audit.service import AuditEntry, write_audit
from devflow.auth.models import User
from devflow.config import Settings
from devflow.db import create_session_factory, lock_admin_set


async def bootstrap_admin(
    factory: async_sessionmaker[AsyncSession], github_id: int
) -> None:
    if type(github_id) is not int or github_id <= 0:
        raise ValueError("A positive GitHub ID is required")
    async with factory.begin() as session:
        await lock_admin_set(session)
        if await session.scalar(select(User.id).limit(1)) is not None:
            return
        user = User(github_id=github_id, role="admin", enabled=True)
        session.add(user)
        await session.flush()
        await write_audit(
            session,
            AuditEntry(
                action="bootstrap.admin",
                resource_type="user",
                resource_id=user.id,
                outcome="success",
            ),
        )


async def main() -> None:
    settings = Settings()
    if settings.bootstrap_admin_github_id is None:
        raise ValueError("BOOTSTRAP_ADMIN_GITHUB_ID is required")
    factory = create_session_factory(settings)
    try:
        await bootstrap_admin(factory, settings.bootstrap_admin_github_id)
    finally:
        await factory.kw["bind"].dispose()


if __name__ == "__main__":
    asyncio.run(main(), loop_factory=asyncio.SelectorEventLoop)
