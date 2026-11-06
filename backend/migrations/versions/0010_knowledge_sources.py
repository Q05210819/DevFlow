"""Immutable knowledge sources, bounded staging and actor-owned import admission."""

import sqlalchemy as sa
from alembic import op

revision = "0010_knowledge_sources"
down_revision = "0009_workspace"
branch_labels = None
depends_on = None


def upgrade():
    op.drop_constraint(op.f("ck_jobs_valid_kind"), "jobs", type_="check")
    op.drop_constraint(op.f("ck_jobs_valid_creator"), "jobs", type_="check")
    op.create_check_constraint(
        "valid_kind",
        "jobs",
        "kind IN ('repository.access_probe','repository.sync','repository.health','repository.workspace','knowledge.import')",
    )
    op.create_check_constraint(
        "valid_creator",
        "jobs",
        "(kind IN ('repository.access_probe','knowledge.import') AND requested_by IS NOT NULL) OR (kind IN ('repository.sync','repository.health','repository.workspace') AND requested_by IS NULL)",
    )
    op.execute("""
CREATE TABLE knowledge_capacity (
	id SERIAL NOT NULL, 
	body_bytes BIGINT DEFAULT '0' NOT NULL, 
	reserved_bytes BIGINT DEFAULT '0' NOT NULL, 
	chunks INTEGER DEFAULT '0' NOT NULL, 
	reserved_chunks INTEGER DEFAULT '0' NOT NULL, 
	CONSTRAINT pk_knowledge_capacity PRIMARY KEY (id), 
	CONSTRAINT ck_knowledge_capacity_singleton CHECK (id = 1), 
	CONSTRAINT ck_knowledge_capacity_bytes CHECK (body_bytes >= 0 AND reserved_bytes >= 0 AND body_bytes + reserved_bytes <= 134217728), 
	CONSTRAINT ck_knowledge_capacity_chunks CHECK (chunks >= 0 AND reserved_chunks >= 0 AND chunks + reserved_chunks <= 40000)
)
    """)
    op.execute("""
CREATE TABLE knowledge_documents (
	id UUID NOT NULL, 
	repository_id UUID NOT NULL, 
	source_repository_id UUID NOT NULL, 
	kind VARCHAR(16) NOT NULL, 
	path VARCHAR(4096), 
	path_digest VARCHAR(32) GENERATED ALWAYS AS (md5(path::text)) STORED,
	attributes JSONB DEFAULT '{}' NOT NULL, 
	deleted BOOLEAN DEFAULT false NOT NULL, 
	CONSTRAINT pk_knowledge_documents PRIMARY KEY (id), 
	CONSTRAINT ck_knowledge_documents_kind CHECK (kind IN ('upload','workspace')), 
	CONSTRAINT ck_knowledge_documents_metadata CHECK (octet_length(attributes::text) <= 8192), 
	CONSTRAINT ck_knowledge_documents_source CHECK ((kind='upload' AND path IS NULL AND repository_id=source_repository_id) OR (kind='workspace' AND path IS NOT NULL AND octet_length(path) BETWEEN 1 AND 4096)), 
	CONSTRAINT fk_knowledge_documents_repository_id_repositories FOREIGN KEY(repository_id) REFERENCES repositories (id), 
	CONSTRAINT fk_knowledge_documents_source_repository_id_repositories FOREIGN KEY(source_repository_id) REFERENCES repositories (id)
)
    """)
    op.execute("""
CREATE TABLE knowledge_repository_capacity (
	repository_id UUID NOT NULL, 
	chunks INTEGER DEFAULT '0' NOT NULL, 
	reserved_chunks INTEGER DEFAULT '0' NOT NULL, 
	CONSTRAINT pk_knowledge_repository_capacity PRIMARY KEY (repository_id), 
	CONSTRAINT ck_knowledge_repository_capacity_chunks CHECK (chunks >= 0 AND reserved_chunks >= 0 AND chunks + reserved_chunks <= 10000), 
	CONSTRAINT fk_knowledge_repository_capacity_repository_id_repositories FOREIGN KEY(repository_id) REFERENCES repositories (id)
)
    """)
    op.execute("""
CREATE TABLE knowledge_corpora (
	repository_id UUID NOT NULL, 
	target_generation BIGINT DEFAULT '0' NOT NULL, 
	target_workspace_id UUID, 
	target_sha VARCHAR(40), 
	path_rule_version VARCHAR(64) DEFAULT 'knowledge-paths-v1' NOT NULL, 
	path_scopes JSONB DEFAULT '{}' NOT NULL, 
	active_index_version_id UUID, 
	CONSTRAINT pk_knowledge_corpora PRIMARY KEY (repository_id), 
	CONSTRAINT ck_knowledge_corpora_generation CHECK (target_generation >= 0), 
	CONSTRAINT ck_knowledge_corpora_sha CHECK (target_sha IS NULL OR target_sha ~ '^[0-9a-f]{40}$'), 
	CONSTRAINT ck_knowledge_corpora_metadata CHECK (octet_length(path_scopes::text) <= 8192), 
	CONSTRAINT fk_knowledge_corpora_repository_id_repositories FOREIGN KEY(repository_id) REFERENCES repositories (id), 
	CONSTRAINT fk_knowledge_corpora_target_workspace_id_workspace_versions FOREIGN KEY(target_workspace_id) REFERENCES workspace_versions (id)
)
    """)
    op.execute("""
CREATE TABLE knowledge_revisions (
	id UUID NOT NULL, 
	document_id UUID NOT NULL, 
	revision_hash VARCHAR(64) NOT NULL, 
	source_version VARCHAR(64) NOT NULL, 
	workspace_id UUID, 
	sha VARCHAR(40), 
	body BYTEA, 
	body_bytes INTEGER NOT NULL, 
	searchable BOOLEAN DEFAULT false NOT NULL, 
	availability VARCHAR(16) DEFAULT 'available' NOT NULL,
	 published_at TIMESTAMP WITH TIME ZONE, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT clock_timestamp() NOT NULL, 
	CONSTRAINT pk_knowledge_revisions PRIMARY KEY (id), 
	CONSTRAINT uq_knowledge_revision UNIQUE (document_id, source_version, revision_hash), 
	CONSTRAINT ck_knowledge_revisions_hashes CHECK (revision_hash ~ '^[0-9a-f]{64}$' AND source_version ~ '^[0-9a-f]{64}$'), 
	CONSTRAINT ck_knowledge_revisions_source CHECK ((workspace_id IS NULL AND sha IS NULL) OR (workspace_id IS NOT NULL AND sha ~ '^[0-9a-f]{40}$')), 
	CONSTRAINT ck_knowledge_revisions_body CHECK (body_bytes BETWEEN 0 AND 1048576 AND (body IS NULL OR octet_length(body)=body_bytes)), 
	CONSTRAINT ck_knowledge_revisions_availability CHECK (availability IN ('available','deleted','expired','unavailable') AND (NOT searchable OR (availability='available' AND body IS NOT NULL AND published_at IS NOT NULL))), 
	CONSTRAINT fk_knowledge_revisions_document_id_knowledge_documents FOREIGN KEY(document_id) REFERENCES knowledge_documents (id), 
	CONSTRAINT fk_knowledge_revisions_workspace_id_workspace_versions FOREIGN KEY(workspace_id) REFERENCES workspace_versions (id)
)
    """)
    op.execute("""
CREATE TABLE knowledge_chunks (
	id UUID NOT NULL, 
	revision_id UUID NOT NULL, 
	ordinal INTEGER NOT NULL, 
	text TEXT NOT NULL, 
	embedding_text TEXT NOT NULL, 
	content_hash VARCHAR(64) NOT NULL, 
	token_count INTEGER NOT NULL, 
	tokenizer_fingerprint VARCHAR(64) NOT NULL, 
	chunking_version VARCHAR(128) NOT NULL, 
	start_line INTEGER NOT NULL, 
	end_line INTEGER NOT NULL, 
	start_byte INTEGER NOT NULL, 
	end_byte INTEGER NOT NULL, 
	attributes JSONB DEFAULT '{}' NOT NULL, 
	CONSTRAINT pk_knowledge_chunks PRIMARY KEY (id), 
	CONSTRAINT uq_knowledge_chunk UNIQUE (revision_id, tokenizer_fingerprint, chunking_version, ordinal), 
	CONSTRAINT ck_knowledge_chunks_tokens CHECK (ordinal >= 0 AND token_count BETWEEN 1 AND 512), 
	CONSTRAINT ck_knowledge_chunks_bytes CHECK (octet_length(embedding_text) <= 8192 AND octet_length(text) <= 8192 AND octet_length(attributes::text) <= 2048), 
	CONSTRAINT ck_knowledge_chunks_location CHECK (start_line >= 1 AND end_line >= start_line AND start_byte >= 0 AND end_byte > start_byte), 
	CONSTRAINT ck_knowledge_chunks_hashes CHECK (content_hash ~ '^[0-9a-f]{64}$' AND tokenizer_fingerprint ~ '^[0-9a-f]{64}$'), 
	CONSTRAINT fk_knowledge_chunks_revision_id_knowledge_revisions FOREIGN KEY(revision_id) REFERENCES knowledge_revisions (id)
)
    """)
    op.execute("""
CREATE TABLE knowledge_imports (
	id UUID NOT NULL, 
	repository_id UUID NOT NULL, 
	source_repository_id UUID NOT NULL, 
	actor_id UUID NOT NULL, 
	actor_github_id BIGINT NOT NULL, 
	actor_auth_version INTEGER NOT NULL, 
	kind VARCHAR(16) NOT NULL, 
	idempotency_key VARCHAR(128) NOT NULL, 
	request_hash VARCHAR(64) NOT NULL, 
	request JSONB NOT NULL, 
	target_generation BIGINT NOT NULL, 
	workspace_id UUID, 
	sha VARCHAR(40), 
	path_rule_version VARCHAR(64) DEFAULT 'knowledge-paths-v1' NOT NULL, 
	job_id UUID NOT NULL, 
	status VARCHAR(16) DEFAULT 'queued' NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	deadline TIMESTAMP WITH TIME ZONE NOT NULL, 
	cursor BIGINT DEFAULT '0' NOT NULL, 
	included INTEGER DEFAULT '0' NOT NULL, 
	skipped INTEGER DEFAULT '0' NOT NULL, 
	unsupported INTEGER DEFAULT '0' NOT NULL, 
	over_limit INTEGER DEFAULT '0' NOT NULL, 
	scanned_bytes BIGINT DEFAULT '0' NOT NULL, 
	failures INTEGER DEFAULT '0' NOT NULL, 
	unscanned BOOLEAN DEFAULT true NOT NULL, 
	complete BOOLEAN DEFAULT false NOT NULL, 
	CONSTRAINT pk_knowledge_imports PRIMARY KEY (id), 
	CONSTRAINT uq_knowledge_request UNIQUE (actor_id, repository_id, idempotency_key), 
	CONSTRAINT uq_knowledge_generation UNIQUE (repository_id, target_generation), 
	CONSTRAINT ck_knowledge_imports_versions CHECK (target_generation >= 1 AND actor_auth_version >= 1 AND actor_github_id > 0), 
	CONSTRAINT ck_knowledge_imports_kind CHECK (kind IN ('upload','workspace')), 
	CONSTRAINT ck_knowledge_imports_status CHECK (status IN ('queued','building','ready','ready_partial','failed','superseded')), 
	CONSTRAINT ck_knowledge_imports_request CHECK (request_hash ~ '^[0-9a-f]{64}$' AND idempotency_key ~ '^[!-~]{1,128}$'), 
	CONSTRAINT ck_knowledge_imports_metadata CHECK (octet_length(request::text) <= 8192), 
	CONSTRAINT ck_knowledge_imports_deadline CHECK (deadline = created_at + interval '24 hours'), 
	CONSTRAINT ck_knowledge_imports_progress CHECK (cursor >= 0 AND included >= 0 AND skipped >= 0 AND unsupported >= 0 AND over_limit >= 0 AND scanned_bytes BETWEEN 0 AND 33554432 AND failures BETWEEN 0 AND 3), 
	CONSTRAINT fk_knowledge_imports_repository_id_knowledge_corpora FOREIGN KEY(repository_id) REFERENCES knowledge_corpora (repository_id), 
	CONSTRAINT fk_knowledge_imports_source_repository_id_repositories FOREIGN KEY(source_repository_id) REFERENCES repositories (id), 
	CONSTRAINT fk_knowledge_imports_actor_id_users FOREIGN KEY(actor_id) REFERENCES users (id), 
	CONSTRAINT fk_knowledge_imports_workspace_id_workspace_versions FOREIGN KEY(workspace_id) REFERENCES workspace_versions (id), 
	CONSTRAINT uq_knowledge_imports_job_id UNIQUE (job_id), 
	CONSTRAINT fk_knowledge_imports_job_id_jobs FOREIGN KEY(job_id) REFERENCES jobs (id)
)
    """)
    op.execute("""
CREATE TABLE knowledge_revision_dependencies (
	revision_id UUID NOT NULL, 
	repository_id UUID NOT NULL, 
	CONSTRAINT pk_knowledge_revision_dependencies PRIMARY KEY (revision_id, repository_id), 
	CONSTRAINT fk_knowledge_revision_dependencies_revision_id_knowledg_f65f FOREIGN KEY(revision_id) REFERENCES knowledge_revisions (id), 
	CONSTRAINT fk_knowledge_revision_dependencies_repository_id_repositories FOREIGN KEY(repository_id) REFERENCES repositories (id)
)
    """)
    op.execute("""
CREATE TABLE knowledge_revision_pins (
	id UUID NOT NULL, 
	revision_id UUID NOT NULL, 
	expires_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	CONSTRAINT pk_knowledge_revision_pins PRIMARY KEY (id), 
	CONSTRAINT fk_knowledge_revision_pins_revision_id_knowledge_revisions FOREIGN KEY(revision_id) REFERENCES knowledge_revisions (id)
)
    """)
    op.execute("""
CREATE TABLE knowledge_revision_retentions (
	owner_kind VARCHAR(16) NOT NULL, 
	owner_id UUID NOT NULL, 
	revision_id UUID NOT NULL, 
	expires_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	CONSTRAINT pk_knowledge_revision_retentions PRIMARY KEY (owner_kind, owner_id, revision_id), 
	CONSTRAINT ck_knowledge_revision_retentions_owner_kind CHECK (owner_kind IN ('query','message')), 
	CONSTRAINT fk_knowledge_revision_retentions_revision_id_knowledge__45bc FOREIGN KEY(revision_id) REFERENCES knowledge_revisions (id)
)
    """)
    op.execute("""
CREATE TABLE knowledge_import_epochs (
	import_id UUID NOT NULL, 
	repository_id UUID NOT NULL, 
	installation_id BIGINT NOT NULL, 
	user_version INTEGER NOT NULL, 
	installation_version INTEGER NOT NULL, 
	repository_version INTEGER NOT NULL, 
	CONSTRAINT pk_knowledge_import_epochs PRIMARY KEY (import_id, repository_id), 
	CONSTRAINT ck_knowledge_import_epochs_versions CHECK (user_version >= 1 AND installation_version >= 1 AND repository_version >= 1), 
	CONSTRAINT fk_knowledge_import_epochs_import_id_knowledge_imports FOREIGN KEY(import_id) REFERENCES knowledge_imports (id), 
	CONSTRAINT fk_knowledge_import_epochs_repository_id_repositories FOREIGN KEY(repository_id) REFERENCES repositories (id), 
	CONSTRAINT fk_knowledge_import_epochs_installation_id_github_installations FOREIGN KEY(installation_id) REFERENCES github_installations (installation_id)
)
    """)
    op.execute("""
CREATE TABLE knowledge_import_members (
	id UUID NOT NULL, 
	import_id UUID NOT NULL, 
	source_key VARCHAR(4096) NOT NULL, 
	source_key_digest VARCHAR(32) GENERATED ALWAYS AS (md5(source_key::text)) STORED NOT NULL,
	ordinal INTEGER NOT NULL, 
	revision_id UUID, 
	status VARCHAR(16) DEFAULT 'pending' NOT NULL, 
	reason VARCHAR(64), 
	CONSTRAINT pk_knowledge_import_members PRIMARY KEY (id), 
	CONSTRAINT uq_knowledge_member UNIQUE (import_id, source_key_digest), 
	CONSTRAINT uq_knowledge_member_ordinal UNIQUE (import_id, ordinal), 
	CONSTRAINT ck_knowledge_import_members_identity CHECK (ordinal >= 0 AND octet_length(source_key) BETWEEN 1 AND 4096), 
	CONSTRAINT ck_knowledge_import_members_status CHECK (status IN ('pending','included','skipped','unsupported','over_limit')), 
	CONSTRAINT fk_knowledge_import_members_import_id_knowledge_imports FOREIGN KEY(import_id) REFERENCES knowledge_imports (id), 
	CONSTRAINT fk_knowledge_import_members_revision_id_knowledge_revisions FOREIGN KEY(revision_id) REFERENCES knowledge_revisions (id)
)
    """)
    op.execute("""
CREATE TABLE knowledge_upload_stages (
	id UUID NOT NULL, 
	actor_id UUID NOT NULL, 
	revision_id UUID NOT NULL, 
	filename VARCHAR(4096) NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	expires_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	consumed_import_id UUID, 
	CONSTRAINT pk_knowledge_upload_stages PRIMARY KEY (id), 
	CONSTRAINT fk_knowledge_upload_stages_actor_id_users FOREIGN KEY(actor_id) REFERENCES users (id), 
	CONSTRAINT fk_knowledge_upload_stages_revision_id_knowledge_revisions FOREIGN KEY(revision_id) REFERENCES knowledge_revisions (id), 
	CONSTRAINT uq_knowledge_upload_stages_consumed_import_id UNIQUE (consumed_import_id), 
	CONSTRAINT fk_knowledge_upload_stages_consumed_import_id_knowledge_imports FOREIGN KEY(consumed_import_id) REFERENCES knowledge_imports (id)
)
    """)
    op.execute("""
CREATE UNIQUE INDEX uq_knowledge_workspace_document ON knowledge_documents (repository_id, source_repository_id, path_digest) WHERE kind='workspace'
    """)
    op.execute("""
CREATE INDEX ix_knowledge_revision_pins_expires_at ON knowledge_revision_pins (expires_at)
    """)
    op.execute("""
CREATE INDEX ix_knowledge_revision_pins_revision_id ON knowledge_revision_pins (revision_id)
    """)
    op.execute("""
CREATE INDEX ix_knowledge_revision_retentions_expires_at ON knowledge_revision_retentions (expires_at)
    """)
    op.execute("""
CREATE INDEX ix_knowledge_upload_stages_expires_at ON knowledge_upload_stages (expires_at)
    """)


def downgrade():
    if op.get_bind().scalar(
        sa.text(
            "SELECT EXISTS(SELECT 1 FROM knowledge_documents) OR EXISTS(SELECT 1 FROM knowledge_corpora) OR EXISTS(SELECT 1 FROM knowledge_revisions) OR EXISTS(SELECT 1 FROM knowledge_chunks) OR EXISTS(SELECT 1 FROM knowledge_imports) OR EXISTS(SELECT 1 FROM knowledge_revision_dependencies) OR EXISTS(SELECT 1 FROM knowledge_revision_pins) OR EXISTS(SELECT 1 FROM knowledge_revision_retentions) OR EXISTS(SELECT 1 FROM knowledge_import_epochs) OR EXISTS(SELECT 1 FROM knowledge_import_members) OR EXISTS(SELECT 1 FROM knowledge_upload_stages) OR EXISTS(SELECT 1 FROM jobs WHERE kind='knowledge.import')"
        )
    ):
        raise RuntimeError("Cannot discard knowledge provenance")
    op.drop_table("knowledge_upload_stages")
    op.drop_table("knowledge_import_members")
    op.drop_table("knowledge_import_epochs")
    op.drop_table("knowledge_revision_retentions")
    op.drop_table("knowledge_revision_pins")
    op.drop_table("knowledge_revision_dependencies")
    op.drop_table("knowledge_imports")
    op.drop_table("knowledge_chunks")
    op.drop_table("knowledge_revisions")
    op.drop_table("knowledge_corpora")
    op.drop_table("knowledge_repository_capacity")
    op.drop_table("knowledge_documents")
    op.drop_table("knowledge_capacity")
    op.drop_constraint(op.f("ck_jobs_valid_kind"), "jobs", type_="check")
    op.drop_constraint(op.f("ck_jobs_valid_creator"), "jobs", type_="check")
    op.create_check_constraint(
        "valid_kind",
        "jobs",
        "kind IN ('repository.access_probe','repository.sync','repository.health','repository.workspace')",
    )
    op.create_check_constraint(
        "valid_creator",
        "jobs",
        "(kind = 'repository.access_probe' AND requested_by IS NOT NULL) OR (kind IN ('repository.sync','repository.health','repository.workspace') AND requested_by IS NULL)",
    )
