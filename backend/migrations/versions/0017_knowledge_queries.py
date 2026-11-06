"""Private query rows, their bounded explicit scope and retained result payload.

The question and selection are immutable. ``manifest`` and ``result`` are the
only bounded, reclaimable payload: a finished query keeps them for one hour,
and a later submission may clear the oldest terminal query in place without
discarding its authorization audit.
"""

from alembic import op

revision = "0017_knowledge_queries"
down_revision = "0016_one_index_build"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
CREATE TABLE knowledge_queries (
	id UUID NOT NULL,
	job_id UUID NOT NULL,
	actor_id UUID NOT NULL,
	question TEXT NOT NULL,
	question_bytes INTEGER NOT NULL,
	selection JSONB NOT NULL,
	manifest JSONB,
	result JSONB,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT clock_timestamp() NOT NULL,
	completed_at TIMESTAMP WITH TIME ZONE,
	retention_expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
	reclaimed_at TIMESTAMP WITH TIME ZONE,
	CONSTRAINT pk_knowledge_queries PRIMARY KEY (id),
	CONSTRAINT uq_knowledge_queries_job UNIQUE (job_id),
	CONSTRAINT ck_knowledge_queries_question CHECK (question_bytes = octet_length(question) AND question_bytes BETWEEN 1 AND 2048),
	CONSTRAINT ck_knowledge_queries_selection CHECK (octet_length(selection::text) <= 16384),
	CONSTRAINT ck_knowledge_queries_manifest CHECK (manifest IS NULL OR octet_length(manifest::text) <= 65536),
	CONSTRAINT ck_knowledge_queries_result CHECK (result IS NULL OR octet_length(result::text) <= 131072),
	CONSTRAINT ck_knowledge_queries_payload CHECK ((manifest IS NULL) = (result IS NULL)),
	CONSTRAINT ck_knowledge_queries_retention CHECK (retention_expires_at = created_at + interval '1 hour'),
	CONSTRAINT fk_knowledge_queries_job_id_jobs FOREIGN KEY(job_id) REFERENCES jobs (id),
	CONSTRAINT fk_knowledge_queries_actor_id_users FOREIGN KEY(actor_id) REFERENCES users (id)
)
    """)
    op.execute("""
CREATE TABLE knowledge_query_repositories (
	query_id UUID NOT NULL,
	repository_id UUID NOT NULL,
	ordinal INTEGER NOT NULL,
	CONSTRAINT pk_knowledge_query_repositories PRIMARY KEY (query_id, repository_id),
	CONSTRAINT uq_knowledge_query_repository_ordinal UNIQUE (query_id, ordinal),
	CONSTRAINT ck_knowledge_query_repositories_ordinal CHECK (ordinal BETWEEN 0 AND 2),
	CONSTRAINT fk_knowledge_query_repositories_query_id_knowledge_queries FOREIGN KEY(query_id) REFERENCES knowledge_queries (id) ON DELETE CASCADE,
	CONSTRAINT fk_knowledge_query_repositories_repository_id_repositories FOREIGN KEY(repository_id) REFERENCES repositories (id)
)
    """)
    op.execute(
        "CREATE INDEX ix_knowledge_queries_actor_created "
        "ON knowledge_queries (actor_id, created_at)"
    )


def downgrade():
    op.drop_table("knowledge_query_repositories")
    op.drop_table("knowledge_queries")
