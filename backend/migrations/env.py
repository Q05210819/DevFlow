"""Run migrations explicitly; API and Worker startup never migrate."""

import os

from alembic import context
from sqlalchemy import create_engine, pool

from devflow.audit import models as audit_models  # noqa: F401
from devflow.auth import models as auth_models  # noqa: F401
from devflow.db import Base
from devflow.github import models as github_models  # noqa: F401

config = context.config
url = config.attributes.get("database_url") or os.environ.get("DATABASE_URL")
if not url:
    raise ValueError("DATABASE_URL is required for migrations")


def configure(connection=None):
    context.configure(
        connection=connection,
        url=url if connection is None else None,
        target_metadata=Base.metadata,
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    configure()
else:
    engine = create_engine(url, poolclass=pool.NullPool, hide_parameters=True)
    try:
        with engine.connect() as connection:
            configure(connection)
    finally:
        engine.dispose()
