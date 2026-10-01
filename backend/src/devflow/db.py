"""PostgreSQL sessions and migration readiness.

The factory is shared; each request, worker operation and heartbeat must open
its own session. Callers own transactions and the engine's lifetime.
"""

from pathlib import Path

from alembic.script import ScriptDirectory
from sqlalchemy import MetaData, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from devflow.config import Settings


class Base(DeclarativeBase):
    metadata = MetaData(
        naming_convention={
            "ix": "ix_%(table_name)s_%(column_0_name)s",
            "uq": "uq_%(table_name)s_%(column_0_name)s",
            "ck": "ck_%(table_name)s_%(constraint_name)s",
            "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
            "pk": "pk_%(table_name)s",
        }
    )


# All bootstrap/member administrator-set changes take this transaction lock.
ADMIN_SET_LOCK_ID = 0x444556464C4F57


async def lock_admin_set(session: AsyncSession) -> None:
    await session.execute(
        text("SELECT pg_advisory_xact_lock(:key)"), {"key": ADMIN_SET_LOCK_ID}
    )


def create_session_factory(settings: Settings) -> async_sessionmaker[AsyncSession]:
    if settings.database_url is None:
        raise ValueError("DATABASE_URL is required")
    url = make_url(settings.database_url.get_secret_value())
    if url.drivername != "postgresql+psycopg":
        raise ValueError("DATABASE_URL must use postgresql+psycopg")
    engine = create_async_engine(
        url,
        pool_pre_ping=True,
        hide_parameters=True,
        connect_args={"connect_timeout": 3},
    )
    return async_sessionmaker(engine, expire_on_commit=False)


async def schema_is_current(session: AsyncSession) -> bool:
    # Read the deployed migration scripts, not a duplicated hard-coded revision.
    scripts = ScriptDirectory(str(Path(__file__).resolve().parents[2] / "migrations"))
    heads = set(scripts.get_heads())
    revisions = set(
        await session.scalars(text("SELECT version_num FROM alembic_version"))
    )
    return bool(heads) and revisions == heads
