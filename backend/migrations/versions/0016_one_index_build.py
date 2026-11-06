"""At most one unfinished index candidate per repository.

Both request paths (explicit and automatic) supersede older candidates before
inserting a new one. Older data may predate that rule, so the migration first
terminalizes every but the newest unfinished candidate per repository. Those
Jobs remain recoverable; their workers observe a terminal row and finish.

Revision ID: 0016_one_index_build
Revises: 0015_knowledge_indexes
"""

from alembic import op

revision = "0016_one_index_build"
down_revision = "0015_knowledge_indexes"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
WITH ranked AS (
    SELECT id,
           row_number() OVER (
               PARTITION BY repository_id
               ORDER BY created_at DESC, id DESC
           ) AS position
    FROM knowledge_index_versions
    WHERE status IN ('queued', 'building')
)
UPDATE knowledge_index_versions AS version
SET status = 'superseded',
    updated_at = clock_timestamp()
FROM ranked
WHERE version.id = ranked.id AND ranked.position > 1
    """)
    op.execute(
        "CREATE UNIQUE INDEX uq_knowledge_index_versions_one_unfinished "
        "ON knowledge_index_versions (repository_id) "
        "WHERE status IN ('queued', 'building')"
    )


def downgrade():
    op.execute("DROP INDEX uq_knowledge_index_versions_one_unfinished")
