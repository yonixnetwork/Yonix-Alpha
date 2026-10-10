"""Entry signals (shadow): the first moment an early-entry strategy or a
baseline wanted a token, what was known then, and its outcome once
labelled. A new, empty table: no backfill, nothing locked.

Revision ID: 0047
Revises: 0046
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0047"
down_revision = "0046"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "entry_signals",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("mint", sa.String(64), nullable=False),
        sa.Column("strategy", sa.String(40), nullable=False),
        sa.Column("lifecycle", sa.String(16), nullable=False),
        sa.Column("decision", sa.String(16), nullable=False),
        sa.Column("phase", sa.String(32), nullable=True),
        sa.Column("score", sa.Numeric(8, 4), nullable=True),
        sa.Column("evidence_level", sa.String(16), nullable=True),
        sa.Column("size_factor", sa.Numeric(6, 4), nullable=True),
        sa.Column("ml_probability", sa.Numeric(8, 6), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("launch_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("price_raw", sa.Numeric(38, 18), nullable=True),
        sa.Column("features", postgresql.JSONB(), nullable=True),
        sa.Column("reasons", postgresql.JSONB(), nullable=True),
        sa.Column("evidence", postgresql.JSONB(), nullable=True),
        sa.Column("candidate_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("outcome", postgresql.JSONB(), nullable=True),
        sa.Column("outcome_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("label_version", sa.String(32), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("mint", "strategy", name="uq_entry_signals_mint_strategy"),
    )
    op.create_index("ix_entry_signals_decided_at", "entry_signals", ["decided_at"])
    op.create_index("ix_entry_signals_strategy_decided_at", "entry_signals", ["strategy", "decided_at"])
    op.create_index("ix_entry_signals_unlabelled", "entry_signals", ["decided_at"],
                    postgresql_where=sa.text("outcome_at IS NULL"))


def downgrade() -> None:
    op.drop_index("ix_entry_signals_unlabelled", table_name="entry_signals")
    op.drop_index("ix_entry_signals_strategy_decided_at", table_name="entry_signals")
    op.drop_index("ix_entry_signals_decided_at", table_name="entry_signals")
    op.drop_table("entry_signals")
