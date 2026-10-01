import asyncio
import os
import subprocess
import sys

import pytest
from sqlalchemy import select, text


@pytest.mark.asyncio
async def test_bootstrap_is_once_and_concurrent_safe(db_factory):
    from devflow.auth.models import User
    from devflow.bootstrap import bootstrap_admin

    barrier = asyncio.Barrier(2)

    async def run(github_id):
        await barrier.wait()
        await bootstrap_admin(db_factory, github_id)

    await asyncio.gather(run(5_000_000_001), run(5_000_000_002))
    async with db_factory.begin() as session:
        users = list(await session.scalars(select(User)))
        assert len(users) == 1
        admin_count = sum(u.role == "admin" and u.enabled for u in users)
        assert admin_count == 1
        users[0].role = "member"
        users[0].enabled = False
        initial_id = users[0].github_id
    await bootstrap_admin(db_factory, 5_000_000_003)
    async with db_factory() as session:
        users = list(await session.scalars(select(User)))
        assert [(u.github_id, u.role, u.enabled) for u in users] == [
            (initial_id, "member", False)
        ]


def test_bootstrap_cli(migrated_database):
    from sqlalchemy import create_engine, text

    url = migrated_database[0].database_url.get_secret_value()
    result = subprocess.run(
        [sys.executable, "-m", "devflow.bootstrap"],
        env=dict(
            os.environ,
            APP_ENV="test",
            PUBLIC_BASE_URL="http://localhost:3000",
            DATABASE_URL=url,
            BOOTSTRAP_ADMIN_GITHUB_ID="5000000004",
        ),
        capture_output=True,
        check=False,
        text=True,
        timeout=15,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert result.returncode == 0, result.stderr
    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            assert (
                connection.scalar(
                    text("SELECT github_id FROM users WHERE role = 'admin'")
                )
                == 5_000_000_004
            )
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_bootstrap_waits_for_shared_admin_lock(db_factory):
    from devflow.auth.models import User
    from devflow.bootstrap import bootstrap_admin
    from devflow.db import lock_admin_set

    # A member-management transaction holds the exact same lock. Observe the
    # bootstrap connection waiting in PostgreSQL instead of guessing with sleep.
    task = None
    try:
        async with db_factory.begin() as admin_session:
            await lock_admin_set(admin_session)
            blocker = await admin_session.scalar(text("SELECT pg_backend_pid()"))
            task = asyncio.create_task(bootstrap_admin(db_factory, 100))
            async with asyncio.timeout(5), db_factory() as observer:
                while not await observer.scalar(
                    text(
                        "SELECT EXISTS (SELECT 1 FROM pg_locks "
                        "WHERE NOT granted AND :blocker = ANY(pg_blocking_pids(pid)))"
                    ),
                    {"blocker": blocker},
                ):
                    if task.done():
                        await task
                        pytest.fail("Bootstrap bypassed the administrator-set lock")
                    await asyncio.sleep(0)
            admin_session.add(User(github_id=99, role="member"))
        await task
        async with db_factory() as session:
            assert list(await session.scalars(select(User.github_id))) == [99]
    finally:
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
