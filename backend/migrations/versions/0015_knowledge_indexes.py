"""Versioned knowledge index manifests and the atomic active pointer.

Vectors stay in Milvus; this revision adds only the PostgreSQL lifecycle. An
existing installation with index rows refuses to downgrade rather than losing
the only record of published vector versions.
"""

import sqlalchemy as sa
from alembic import op

revision = "0015_knowledge_indexes"
down_revision = "0014_model_jobs"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
CREATE TABLE knowledge_index_versions (
	id UUID NOT NULL,
	repository_id UUID NOT NULL,
	import_id UUID NOT NULL,
	job_id UUID NOT NULL,
	requested_by UUID NOT NULL,
	target_generation BIGINT NOT NULL,
	target_sha VARCHAR(40),
	path_rule_version VARCHAR(64) DEFAULT 'knowledge-paths-v1' NOT NULL,
	path_scopes JSONB DEFAULT '{}' NOT NULL,
	idempotency_key VARCHAR(128) NOT NULL,
	request_hash VARCHAR(64) NOT NULL,
	authorization_scope JSONB NOT NULL,
	status VARCHAR(16) DEFAULT 'queued' NOT NULL,
	fingerprint JSONB DEFAULT '{}' NOT NULL,
	expected_member_count INTEGER NOT NULL,
	written_count INTEGER DEFAULT 0 NOT NULL,
	member_digest VARCHAR(64) NOT NULL,
	queryable BOOLEAN DEFAULT false NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT clock_timestamp() NOT NULL,
	deadline TIMESTAMP WITH TIME ZONE NOT NULL,
	failures INTEGER DEFAULT 0 NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT clock_timestamp() NOT NULL,
	published_at TIMESTAMP WITH TIME ZONE,
	retired_at TIMESTAMP WITH TIME ZONE,
	purged_at TIMESTAMP WITH TIME ZONE,
	CONSTRAINT pk_knowledge_index_versions PRIMARY KEY (id),
	CONSTRAINT uq_knowledge_index_request UNIQUE (repository_id, idempotency_key),
	CONSTRAINT uq_knowledge_index_job UNIQUE (job_id),
	CONSTRAINT ck_knowledge_index_versions_versions CHECK (target_generation >= 1 AND (target_sha IS NULL OR target_sha ~ '^[0-9a-f]{40}$')),
	CONSTRAINT ck_knowledge_index_versions_status CHECK (status IN ('queued','building','ready','retired','failed','superseded')),
	CONSTRAINT ck_knowledge_index_versions_members CHECK (expected_member_count >= 0 AND written_count BETWEEN 0 AND expected_member_count),
	CONSTRAINT ck_knowledge_index_versions_deadline CHECK (deadline = created_at + interval '24 hours'),
	CONSTRAINT ck_knowledge_index_versions_failures CHECK (failures BETWEEN 0 AND 3),
	CONSTRAINT ck_knowledge_index_versions_digest CHECK (member_digest ~ '^[0-9a-f]{64}$'),
	CONSTRAINT ck_knowledge_index_versions_request CHECK (idempotency_key ~ '^[!-~]{1,128}$' AND request_hash ~ '^[0-9a-f]{64}$'),
	CONSTRAINT ck_knowledge_index_versions_metadata CHECK (octet_length(authorization_scope::text) <= 16384 AND octet_length(path_scopes::text) <= 8192 AND octet_length(fingerprint::text) <= 4096),
	CONSTRAINT ck_knowledge_index_versions_published CHECK (status NOT IN ('ready','retired') OR published_at IS NOT NULL),
	CONSTRAINT ck_knowledge_index_versions_queryable CHECK (NOT queryable OR status = 'ready'),
	CONSTRAINT fk_knowledge_index_versions_repository_id_repositories FOREIGN KEY(repository_id) REFERENCES repositories (id),
	CONSTRAINT fk_knowledge_index_versions_import_id_knowledge_imports FOREIGN KEY(import_id) REFERENCES knowledge_imports (id),
	CONSTRAINT fk_knowledge_index_versions_job_id_jobs FOREIGN KEY(job_id) REFERENCES jobs (id),
	CONSTRAINT fk_knowledge_index_versions_requested_by_users FOREIGN KEY(requested_by) REFERENCES users (id)
)
    """)
    op.execute("""
CREATE TABLE knowledge_index_members (
	id UUID NOT NULL,
	index_version_id UUID NOT NULL,
	chunk_id UUID NOT NULL,
	ordinal INTEGER NOT NULL,
	status VARCHAR(16) DEFAULT 'pending' NOT NULL,
	written_at TIMESTAMP WITH TIME ZONE,
	CONSTRAINT pk_knowledge_index_members PRIMARY KEY (id),
	CONSTRAINT uq_knowledge_index_member UNIQUE (index_version_id, chunk_id),
	CONSTRAINT uq_knowledge_index_member_ordinal UNIQUE (index_version_id, ordinal),
	CONSTRAINT ck_knowledge_index_members_ordinal CHECK (ordinal >= 0),
	CONSTRAINT ck_knowledge_index_members_status CHECK (status IN ('pending','written')),
	CONSTRAINT fk_knowledge_index_members_index_version_id_knowledge_index_versions FOREIGN KEY(index_version_id) REFERENCES knowledge_index_versions (id) ON DELETE CASCADE
)
    """)
    op.execute(
        "CREATE INDEX ix_knowledge_index_versions_repository_status "
        "ON knowledge_index_versions (repository_id, status, created_at)"
    )
    op.execute(
        "CREATE INDEX ix_knowledge_index_versions_purged "
        "ON knowledge_index_versions (purged_at, created_at)"
    )
    op.execute(
        "ALTER TABLE knowledge_corpora ADD CONSTRAINT "
        "fk_knowledge_corpora_active_index_version "
        "FOREIGN KEY(active_index_version_id) "
        "REFERENCES knowledge_index_versions (id)"
    )


def downgrade():
    if op.get_bind().scalar(
        sa.text(
            "SELECT EXISTS(SELECT 1 FROM knowledge_index_versions)"
            " OR EXISTS(SELECT 1 FROM knowledge_index_members)"
            " OR EXISTS(SELECT 1 FROM knowledge_corpora"
            " WHERE active_index_version_id IS NOT NULL)"
        )
    ):
        raise RuntimeError("Cannot discard published knowledge index versions")
    op.execute(
        "ALTER TABLE knowledge_corpora DROP CONSTRAINT "
        "fk_knowledge_corpora_active_index_version"
    )
    op.drop_table("knowledge_index_members")
    op.drop_table("knowledge_index_versions")
