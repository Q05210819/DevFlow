"""Register the model-worker Job kinds.

All three kinds retain a user/actor creator: index builds carry the requesting
admin's authorization source, while queries and chat runs are private to their
creator. This migration only widens the existing constraints; handlers and
their terminal transactions belong to later tasks.
"""

import sqlalchemy as sa
from alembic import op

revision = "0014_model_jobs"
down_revision = "0013_knowledge_cleanup"
branch_labels = None
depends_on = None

_OLD_KINDS = (
    "'repository.access_probe','repository.sync','repository.health',"
    "'repository.workspace','knowledge.import','knowledge.cleanup'"
)
_NEW_KINDS = _OLD_KINDS + ",'knowledge.index_batch','knowledge.query','chat.run'"
_OLD_CREATORS = (
    "(kind IN ('repository.access_probe','knowledge.import')"
    " AND requested_by IS NOT NULL)"
    " OR (kind IN ('repository.sync','repository.health',"
    "'repository.workspace','knowledge.cleanup')"
    " AND requested_by IS NULL)"
)
_NEW_CREATORS = (
    "(kind IN ('repository.access_probe','knowledge.import',"
    "'knowledge.index_batch','knowledge.query','chat.run')"
    " AND requested_by IS NOT NULL)"
    " OR (kind IN ('repository.sync','repository.health',"
    "'repository.workspace','knowledge.cleanup')"
    " AND requested_by IS NULL)"
)


def upgrade():
    op.drop_constraint("valid_kind", "jobs", type_="check")
    op.create_check_constraint("valid_kind", "jobs", f"kind IN ({_NEW_KINDS})")
    op.drop_constraint("valid_creator", "jobs", type_="check")
    op.create_check_constraint("valid_creator", "jobs", _NEW_CREATORS)


def downgrade():
    model_jobs = op.get_bind().scalar(
        sa.text(
            "SELECT EXISTS(SELECT 1 FROM jobs WHERE kind IN "
            "('knowledge.index_batch','knowledge.query','chat.run'))"
        )
    )
    if model_jobs:
        raise RuntimeError("Cannot discard model Worker jobs")
    op.drop_constraint("valid_creator", "jobs", type_="check")
    op.create_check_constraint("valid_creator", "jobs", _OLD_CREATORS)
    op.drop_constraint("valid_kind", "jobs", type_="check")
    op.create_check_constraint("valid_kind", "jobs", f"kind IN ({_OLD_KINDS})")
