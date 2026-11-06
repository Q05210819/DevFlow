"""Private conversations, durable runs, tools and bounded budget evidence.

A conversation freezes its explicit source set; runs carry the Job that leases
their execution and the counters/deadlines that survive a worker restart.
Model payloads, checkpoints and retained evidence live elsewhere; every table
here is bounded metadata. Downgrading would discard private user content and
is refused while any conversation exists.
"""

import sqlalchemy as sa
from alembic import op

revision = "0018_private_chat"
down_revision = "0017_knowledge_queries"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
CREATE TABLE chat_conversations (
	id UUID NOT NULL,
	actor_id UUID NOT NULL,
	idempotency_key VARCHAR(128) NOT NULL,
	input_hash VARCHAR(64) NOT NULL,
	archived_at TIMESTAMP WITH TIME ZONE,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT clock_timestamp() NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT clock_timestamp() NOT NULL,
	CONSTRAINT pk_chat_conversations PRIMARY KEY (id),
	CONSTRAINT uq_chat_conversations_actor_key UNIQUE (actor_id, idempotency_key),
	CONSTRAINT ck_chat_conversations_key CHECK (idempotency_key ~ '^[!-~]{1,128}$'),
	CONSTRAINT ck_chat_conversations_input_hash CHECK (input_hash ~ '^[0-9a-f]{64}$'),
	CONSTRAINT fk_chat_conversations_actor_id_users FOREIGN KEY(actor_id) REFERENCES users (id)
)
    """)
    op.execute("""
CREATE TABLE chat_conversation_repositories (
	conversation_id UUID NOT NULL,
	repository_id UUID NOT NULL,
	ordinal INTEGER NOT NULL,
	CONSTRAINT pk_chat_conversation_repositories PRIMARY KEY (conversation_id, repository_id),
	CONSTRAINT uq_chat_conversation_repository_ordinal UNIQUE (conversation_id, ordinal),
	CONSTRAINT ck_chat_conversation_repositories_ordinal CHECK (ordinal BETWEEN 0 AND 2),
	CONSTRAINT fk_chat_conversation_repositories_conversation_id_chat_conversations FOREIGN KEY(conversation_id) REFERENCES chat_conversations (id) ON DELETE CASCADE,
	CONSTRAINT fk_chat_conversation_repositories_repository_id_repositories FOREIGN KEY(repository_id) REFERENCES repositories (id)
)
    """)
    op.execute("""
CREATE TABLE chat_agent_runs (
	id UUID NOT NULL,
	conversation_id UUID NOT NULL,
	job_id UUID NOT NULL,
	actor_id UUID NOT NULL,
	thread_id UUID NOT NULL,
	graph_version VARCHAR(64) NOT NULL,
	selection JSONB NOT NULL,
	scope JSONB,
	status VARCHAR(16) DEFAULT 'queued' NOT NULL,
	nodes_used INTEGER DEFAULT '0' NOT NULL,
	retrievals_used INTEGER DEFAULT '0' NOT NULL,
	model_requests_used INTEGER DEFAULT '0' NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT clock_timestamp() NOT NULL,
	queued_deadline TIMESTAMP WITH TIME ZONE NOT NULL,
	started_at TIMESTAMP WITH TIME ZONE,
	deadline TIMESTAMP WITH TIME ZONE,
	accepted_config JSONB,
	accepted_retired_at TIMESTAMP WITH TIME ZONE,
	terminal_reason VARCHAR(64),
	terminal_at TIMESTAMP WITH TIME ZONE,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT clock_timestamp() NOT NULL,
	CONSTRAINT pk_chat_agent_runs PRIMARY KEY (id),
	CONSTRAINT uq_chat_agent_runs_job UNIQUE (job_id),
	CONSTRAINT uq_chat_agent_runs_thread UNIQUE (thread_id),
	CONSTRAINT ck_chat_agent_runs_status CHECK (status IN ('queued','running','succeeded','failed')),
	CONSTRAINT ck_chat_agent_runs_terminal CHECK ((status IN ('succeeded','failed')) = (terminal_at IS NOT NULL)),
	CONSTRAINT ck_chat_agent_runs_deadline CHECK ((started_at IS NULL) = (deadline IS NULL)),
	CONSTRAINT ck_chat_agent_runs_budget CHECK (nodes_used BETWEEN 0 AND 8 AND retrievals_used BETWEEN 0 AND 2 AND model_requests_used BETWEEN 0 AND 2),
	CONSTRAINT ck_chat_agent_runs_selection CHECK (octet_length(selection::text) <= 16384),
	CONSTRAINT ck_chat_agent_runs_scope CHECK (scope IS NULL OR octet_length(scope::text) <= 65536),
	CONSTRAINT ck_chat_agent_runs_config CHECK (accepted_config IS NULL OR octet_length(accepted_config::text) <= 65536),
	CONSTRAINT fk_chat_agent_runs_conversation_id_chat_conversations FOREIGN KEY(conversation_id) REFERENCES chat_conversations (id),
	CONSTRAINT fk_chat_agent_runs_job_id_jobs FOREIGN KEY(job_id) REFERENCES jobs (id),
	CONSTRAINT fk_chat_agent_runs_actor_id_users FOREIGN KEY(actor_id) REFERENCES users (id)
)
    """)
    op.execute(
        "CREATE UNIQUE INDEX uq_chat_agent_runs_active_conversation "
        "ON chat_agent_runs (conversation_id) WHERE terminal_at IS NULL"
    )
    op.execute(
        "CREATE INDEX ix_chat_agent_runs_conversation_created "
        "ON chat_agent_runs (conversation_id, created_at)"
    )
    op.execute("""
CREATE TABLE chat_messages (
	id UUID NOT NULL,
	conversation_id UUID NOT NULL,
	run_id UUID,
	ordinal INTEGER NOT NULL,
	role VARCHAR(16) NOT NULL,
	body TEXT NOT NULL,
	body_bytes INTEGER NOT NULL,
	manifest JSONB,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT clock_timestamp() NOT NULL,
	CONSTRAINT pk_chat_messages PRIMARY KEY (id),
	CONSTRAINT uq_chat_messages_conversation_ordinal UNIQUE (conversation_id, ordinal),
	CONSTRAINT ck_chat_messages_ordinal CHECK (ordinal >= 0),
	CONSTRAINT ck_chat_messages_role CHECK (role IN ('user','assistant')),
	CONSTRAINT ck_chat_messages_body CHECK (body_bytes = octet_length(body) AND ((role='user' AND body_bytes BETWEEN 1 AND 2048) OR (role='assistant' AND body_bytes BETWEEN 0 AND 16384))),
	CONSTRAINT ck_chat_messages_manifest CHECK (manifest IS NULL OR octet_length(manifest::text) <= 65536),
	CONSTRAINT fk_chat_messages_conversation_id_chat_conversations FOREIGN KEY(conversation_id) REFERENCES chat_conversations (id) ON DELETE CASCADE,
	CONSTRAINT fk_chat_messages_run_id_chat_agent_runs FOREIGN KEY(run_id) REFERENCES chat_agent_runs (id)
)
    """)
    op.execute(
        "CREATE UNIQUE INDEX uq_chat_messages_run_role "
        "ON chat_messages (run_id, role) WHERE run_id IS NOT NULL"
    )
    op.execute("""
CREATE TABLE chat_tool_calls (
	id UUID NOT NULL,
	run_id UUID NOT NULL,
	ordinal INTEGER NOT NULL,
	kind VARCHAR(32) NOT NULL,
	arguments JSONB NOT NULL,
	result JSONB,
	status VARCHAR(16) DEFAULT 'requested' NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT clock_timestamp() NOT NULL,
	completed_at TIMESTAMP WITH TIME ZONE,
	expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
	CONSTRAINT pk_chat_tool_calls PRIMARY KEY (id),
	CONSTRAINT uq_chat_tool_calls_run_ordinal UNIQUE (run_id, ordinal),
	CONSTRAINT ck_chat_tool_calls_ordinal CHECK (ordinal BETWEEN 0 AND 3),
	CONSTRAINT ck_chat_tool_calls_status CHECK (status IN ('requested','succeeded','failed')),
	CONSTRAINT ck_chat_tool_calls_arguments CHECK (octet_length(arguments::text) <= 16384),
	CONSTRAINT ck_chat_tool_calls_result CHECK (result IS NULL OR octet_length(result::text) <= 131072),
	CONSTRAINT ck_chat_tool_calls_completed CHECK ((status IN ('succeeded','failed')) = (completed_at IS NOT NULL)),
	CONSTRAINT fk_chat_tool_calls_run_id_chat_agent_runs FOREIGN KEY(run_id) REFERENCES chat_agent_runs (id) ON DELETE CASCADE
)
    """)
    op.execute("""
CREATE TABLE chat_budget_ledgers (
	id UUID NOT NULL,
	ledger_scope VARCHAR(8) NOT NULL,
	actor_id UUID NOT NULL,
	run_id UUID,
	month_start DATE,
	currency VARCHAR(3) NOT NULL,
	reserved NUMERIC(24, 6) DEFAULT '0' NOT NULL,
	settled NUMERIC(24, 6) DEFAULT '0' NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT clock_timestamp() NOT NULL,
	CONSTRAINT pk_chat_budget_ledgers PRIMARY KEY (id),
	CONSTRAINT uq_chat_budget_ledgers_run UNIQUE (run_id),
	CONSTRAINT uq_chat_budget_ledgers_month UNIQUE (actor_id, month_start, currency),
	CONSTRAINT ck_chat_budget_ledgers_scope CHECK (ledger_scope IN ('run','month')),
	CONSTRAINT ck_chat_budget_ledgers_shape CHECK ((ledger_scope='run' AND run_id IS NOT NULL AND month_start IS NULL) OR (ledger_scope='month' AND run_id IS NULL AND month_start IS NOT NULL)),
	CONSTRAINT ck_chat_budget_ledgers_currency CHECK (currency ~ '^[A-Z]{3}$'),
	CONSTRAINT ck_chat_budget_ledgers_amounts CHECK (reserved >= 0 AND settled >= 0),
	CONSTRAINT fk_chat_budget_ledgers_actor_id_users FOREIGN KEY(actor_id) REFERENCES users (id),
	CONSTRAINT fk_chat_budget_ledgers_run_id_chat_agent_runs FOREIGN KEY(run_id) REFERENCES chat_agent_runs (id)
)
    """)
    op.execute("""
CREATE TABLE chat_request_receipts (
	id UUID NOT NULL,
	run_id UUID NOT NULL,
	kind VARCHAR(16) DEFAULT 'model' NOT NULL,
	ordinal INTEGER NOT NULL,
	request_hash VARCHAR(64) NOT NULL,
	parameter_version VARCHAR(64) NOT NULL,
	status VARCHAR(16) DEFAULT 'reserved' NOT NULL,
	input_cap BIGINT NOT NULL,
	output_cap INTEGER NOT NULL,
	amount NUMERIC(24, 6),
	currency VARCHAR(3),
	pricing_version VARCHAR(64),
	provider_request_id VARCHAR(128),
	usage JSONB,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT clock_timestamp() NOT NULL,
	dispatched_at TIMESTAMP WITH TIME ZONE,
	settled_at TIMESTAMP WITH TIME ZONE,
	CONSTRAINT pk_chat_request_receipts PRIMARY KEY (id),
	CONSTRAINT uq_chat_request_receipts_logical UNIQUE (run_id, kind, ordinal),
	CONSTRAINT ck_chat_request_receipts_kind CHECK (kind IN ('model')),
	CONSTRAINT ck_chat_request_receipts_ordinal CHECK (ordinal >= 0),
	CONSTRAINT ck_chat_request_receipts_status CHECK (status IN ('reserved','dispatched','settled','unknown')),
	CONSTRAINT ck_chat_request_receipts_hash CHECK (request_hash ~ '^[0-9a-f]{64}$'),
	CONSTRAINT ck_chat_request_receipts_caps CHECK (input_cap > 0 AND output_cap > 0),
	CONSTRAINT ck_chat_request_receipts_amount CHECK ((amount IS NULL) = (currency IS NULL) AND (amount IS NULL OR amount >= 0)),
	CONSTRAINT ck_chat_request_receipts_usage CHECK (usage IS NULL OR octet_length(usage::text) <= 4096),
	CONSTRAINT fk_chat_request_receipts_run_id_chat_agent_runs FOREIGN KEY(run_id) REFERENCES chat_agent_runs (id) ON DELETE CASCADE
)
    """)


def downgrade():
    conversations = op.get_bind().scalar(
        sa.text("SELECT EXISTS(SELECT 1 FROM chat_conversations)")
    )
    if conversations:
        raise RuntimeError("Cannot discard private conversations")
    op.drop_table("chat_request_receipts")
    op.drop_table("chat_budget_ledgers")
    op.drop_table("chat_tool_calls")
    op.drop_table("chat_messages")
    op.drop_table("chat_agent_runs")
    op.drop_table("chat_conversation_repositories")
    op.drop_table("chat_conversations")
