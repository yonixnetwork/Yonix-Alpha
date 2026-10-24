"""Large-buy observation: large buys on Pump.fun curves and their matched
control moments, with the market state before and after
(yonixalpha_core.large_trades). One new, empty table; nothing existing is
changed, scanned or locked.

Revision ID: 0049
Revises: 0048
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0049"
down_revision = "0048"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "large_trade_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("kind", sa.String(8), nullable=False),
        sa.Column("pair_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("mint", sa.String(64), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("wallet", sa.String(64), nullable=True),
        sa.Column("sol", sa.Numeric(20, 9), nullable=True),
        sa.Column("rel_liquidity", sa.Numeric(12, 6), nullable=True),
        sa.Column("rel_volume", sa.Numeric(12, 4), nullable=True),
        sa.Column("pre", postgresql.JSONB(), nullable=False),
        sa.Column("post", postgresql.JSONB(), nullable=True),
        sa.Column("status", sa.String(12), nullable=False),
        sa.Column("definition_version", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("kind IN ('EVENT','CONTROL')", name="ck_large_trade_events_kind"),
        sa.CheckConstraint("status IN ('TRACKING','COMPLETE','INCOMPLETE')", name="ck_large_trade_events_status"),
        sa.UniqueConstraint("kind", "mint", "at", name="uq_large_trade_events_kind_mint_at"),
    )
    op.create_index("ix_large_trade_events_status_at", "large_trade_events", ["status", "at"])
    op.create_index("ix_large_trade_events_at", "large_trade_events", ["at"])
    op.create_index("ix_large_trade_events_pair_id", "large_trade_events", ["pair_id"])


def downgrade() -> None:
    op.drop_index("ix_large_trade_events_pair_id", table_name="large_trade_events")
    op.drop_index("ix_large_trade_events_at", table_name="large_trade_events")
    op.drop_index("ix_large_trade_events_status_at", table_name="large_trade_events")
    op.drop_table("large_trade_events")
