"""Account official chat threads and register the cleanup Job kind.

``chat_checkpoint_accounts`` is recomputed from the official LangGraph tables
inside every fenced write transaction; it is accounting evidence, never an
access pointer. The separate ``chat.cleanup`` kind has no user creator because
it is a bounded system sweep over already terminal runs.
"""

import sqlalchemy as sa
from alembic import op

revision = "0020_chat_retention"
down_revision = "0019_chat_budget"
branch_labels = None
depends_on = None

_OLD_KINDS = (
    "'repository.access_probe','repository.sync','repository.health',"
    "'repository.workspace','knowledge.import','knowledge.cleanup',"
    "'knowledge.index_batch','knowledge.query','chat.run'"
)
_NEW_KINDS = _OLD_KINDS + ",'chat.cleanup'"
_OLD_CREATORS = (
    "(kind IN ('repository.access_probe','knowledge.import',"
    "'knowledge.index_batch','knowledge.query','chat.run')"
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
    "'repository.workspace','knowledge.cleanup','chat.cleanup')"
    " AND requested_by IS NULL)"
)


def upgrade():
    op.execute("""
CREATE TABLE chat_checkpoint_accounts (
	run_id UUID NOT NULL,
	thread_id UUID NOT NULL,
	checkpoint_count INTEGER DEFAULT '0' NOT NULL,
	bytes BIGINT DEFAULT '0' NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT clock_timestamp() NOT NULL,
	CONSTRAINT pk_chat_checkpoint_accounts PRIMARY KEY (run_id),
	CONSTRAINT uq_chat_checkpoint_accounts_thread UNIQUE (thread_id),
	CONSTRAINT ck_chat_checkpoint_accounts_count CHECK (checkpoint_count BETWEEN 0 AND 32),
	CONSTRAINT ck_chat_checkpoint_accounts_bytes CHECK (bytes >= 0),
	CONSTRAINT fk_chat_checkpoint_accounts_run_id_chat_agent_runs FOREIGN KEY(run_id) REFERENCES chat_agent_runs (id) ON DELETE CASCADE
)
    """)
    op.drop_constraint("valid_kind", "jobs", type_="check")
    op.create_check_constraint("valid_kind", "jobs", f"kind IN ({_NEW_KINDS})")
    op.drop_constraint("valid_creator", "jobs", type_="check")
    op.create_check_constraint("valid_creator", "jobs", _NEW_CREATORS)


def downgrade():
    cleanup_jobs = op.get_bind().scalar(
        sa.text("SELECT EXISTS(SELECT 1 FROM jobs WHERE kind = 'chat.cleanup')")
    )
    if cleanup_jobs:
        raise RuntimeError("Cannot discard chat cleanup jobs")
    op.drop_constraint("valid_creator", "jobs", type_="check")
    op.create_check_constraint("valid_creator", "jobs", _OLD_CREATORS)
    op.drop_constraint("valid_kind", "jobs", type_="check")
    op.create_check_constraint("valid_kind", "jobs", f"kind IN ({_OLD_KINDS})")
    op.execute("DROP TABLE chat_checkpoint_accounts")
