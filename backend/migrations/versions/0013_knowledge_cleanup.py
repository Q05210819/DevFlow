"""Register the bounded knowledge cleanup system Job kind."""

import sqlalchemy as sa
from alembic import op

revision = "0013_knowledge_cleanup"
down_revision = "0012_knowledge_stages"
branch_labels = None
depends_on = None

_OLD_KINDS = (
    "'repository.access_probe','repository.sync','repository.health',"
    "'repository.workspace','knowledge.import'"
)
_NEW_KINDS = _OLD_KINDS + ",'knowledge.cleanup'"


def upgrade():
    op.drop_constraint("valid_kind", "jobs", type_="check")
    op.create_check_constraint("valid_kind", "jobs", f"kind IN ({_NEW_KINDS})")
    op.drop_constraint("valid_creator", "jobs", type_="check")
    op.create_check_constraint(
        "valid_creator",
        "jobs",
        "(kind IN ('repository.access_probe','knowledge.import')"
        " AND requested_by IS NOT NULL)"
        " OR (kind IN ('repository.sync','repository.health',"
        "'repository.workspace','knowledge.cleanup')"
        " AND requested_by IS NULL)",
    )


def downgrade():
    cleanup = op.get_bind().scalar(
        sa.text("SELECT EXISTS(SELECT 1 FROM jobs WHERE kind='knowledge.cleanup')")
    )
    if cleanup:
        raise RuntimeError("Cannot discard knowledge cleanup jobs")
    op.drop_constraint("valid_creator", "jobs", type_="check")
    op.create_check_constraint(
        "valid_creator",
        "jobs",
        "(kind IN ('repository.access_probe','knowledge.import')"
        " AND requested_by IS NOT NULL)"
        " OR (kind IN ('repository.sync','repository.health',"
        "'repository.workspace')"
        " AND requested_by IS NULL)",
    )
    op.drop_constraint("valid_kind", "jobs", type_="check")
    op.create_check_constraint("valid_kind", "jobs", f"kind IN ({_OLD_KINDS})")
