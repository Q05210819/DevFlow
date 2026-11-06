"""Reserve chat calls against an accepted price sheet.

Each new receipt stores the complete lease fence, the UTC month bucket and the
exact price snapshot that produced its amount. Rows written before this
migration stay all-NULL: they predate the ledger, are never reconcilable and
must not be guessed at. The run foreign key becomes RESTRICT so deleting user
content can never erase a possible charge or release an unknown reservation.
"""

from alembic import op

revision = "0019_chat_budget"
down_revision = "0018_private_chat"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
ALTER TABLE chat_request_receipts
    ADD COLUMN lease_owner UUID,
    ADD COLUMN lease_generation INTEGER,
    ADD COLUMN lease_attempt INTEGER,
    ADD COLUMN month_start DATE,
    ADD COLUMN minor_unit NUMERIC(24, 6),
    ADD COLUMN input_price_per_million NUMERIC(24, 6),
    ADD COLUMN output_price_per_million NUMERIC(24, 6),
    ADD CONSTRAINT ck_chat_request_receipts_fence CHECK (
        (lease_owner IS NULL AND lease_generation IS NULL
         AND lease_attempt IS NULL AND month_start IS NULL
         AND minor_unit IS NULL AND input_price_per_million IS NULL
         AND output_price_per_million IS NULL)
        OR
        (lease_owner IS NOT NULL AND lease_generation IS NOT NULL
         AND lease_attempt IS NOT NULL AND month_start IS NOT NULL
         AND minor_unit IS NOT NULL AND input_price_per_million IS NOT NULL
         AND output_price_per_million IS NOT NULL
         AND lease_generation >= 0 AND lease_attempt >= 0
         AND minor_unit > 0 AND input_price_per_million > 0
         AND output_price_per_million > 0)
    )
    """)
    op.execute("""
ALTER TABLE chat_request_receipts
    DROP CONSTRAINT fk_chat_request_receipts_run_id_chat_agent_runs,
    ADD CONSTRAINT fk_chat_request_receipts_run_id_chat_agent_runs
        FOREIGN KEY(run_id) REFERENCES chat_agent_runs (id) ON DELETE RESTRICT
    """)


def downgrade():
    op.execute("""
ALTER TABLE chat_request_receipts
    DROP CONSTRAINT fk_chat_request_receipts_run_id_chat_agent_runs,
    ADD CONSTRAINT fk_chat_request_receipts_run_id_chat_agent_runs
        FOREIGN KEY(run_id) REFERENCES chat_agent_runs (id) ON DELETE CASCADE
    """)
    op.execute("""
ALTER TABLE chat_request_receipts
    DROP CONSTRAINT ck_chat_request_receipts_fence,
    DROP COLUMN output_price_per_million,
    DROP COLUMN input_price_per_million,
    DROP COLUMN minor_unit,
    DROP COLUMN month_start,
    DROP COLUMN lease_attempt,
    DROP COLUMN lease_generation,
    DROP COLUMN lease_owner
    """)
