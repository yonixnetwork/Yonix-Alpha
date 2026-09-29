"""Multi-chain P3: EVM discovery (tokens, trades, scan cursors) and a guard
against duplicate open EVM paper positions. All additive.

Revision ID: 0022
Revises: 0021
Create Date: 2026-09-29
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0022"
down_revision: Union[str, None] = "0021"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "evm_tokens",
        sa.Column("chain", sa.String(16), primary_key=True),
        sa.Column("token", sa.String(42), primary_key=True),
        sa.Column("launchpad", sa.String(32), nullable=False),
        sa.Column("creator", sa.String(42), nullable=True),
        sa.Column("name", sa.String(128), nullable=True),
        sa.Column("symbol", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_block", sa.BigInteger(), nullable=True),
        sa.Column("created_tx", sa.String(66), nullable=True),
        sa.Column("quote_token", sa.String(42), nullable=True),
        sa.Column("venue", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("category", sa.String(16), nullable=False, server_default="FRESH"),
        sa.Column("stage", sa.String(16), nullable=False, server_default="CURVE"),
        sa.Column("migrated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("migration", postgresql.JSONB(), nullable=True),
        sa.Column("state", postgresql.JSONB(), nullable=True),
        sa.Column("state_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("safety_verdict", sa.String(8), nullable=True),
        sa.Column("safety", postgresql.JSONB(), nullable=True),
        sa.Column("safety_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("stats", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("last_trade_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("extra", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    for col in ("launchpad", "creator", "created_at", "category", "last_trade_at"):
        op.create_index(f"ix_evm_tokens_{col}", "evm_tokens", [col])
    op.create_table(
        "evm_trades",
        sa.Column("event_id", sa.String(96), primary_key=True),
        sa.Column("chain", sa.String(16), nullable=False),
        sa.Column("launchpad", sa.String(32), nullable=False),
        sa.Column("token", sa.String(42), nullable=False),
        sa.Column("trader", sa.String(42), nullable=False),
        sa.Column("is_buy", sa.Boolean(), nullable=False),
        sa.Column("token_amount", sa.Numeric(78, 0), nullable=False),
        sa.Column("quote_amount", sa.Numeric(78, 0), nullable=False),
        sa.Column("fee", sa.Numeric(78, 0), nullable=True),
        sa.Column("block", sa.BigInteger(), nullable=True),
        sa.Column("tx_hash", sa.String(66), nullable=True),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("extra", postgresql.JSONB(), nullable=True),
    )
    op.create_index("ix_evm_trades_token_at", "evm_trades", ["chain", "token", "at"])
    op.create_index("ix_evm_trades_trader_at", "evm_trades", ["trader", "at"])
    op.create_index("ix_evm_trades_at", "evm_trades", ["at"])
    op.create_table(
        "evm_cursors",
        sa.Column("chain", sa.String(16), primary_key=True),
        sa.Column("launchpad", sa.String(32), primary_key=True),
        sa.Column("last_block", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("uq_paper_positions_evm_open", "paper_positions", ["engine", "asset_id"], unique=True,
                    postgresql_where=sa.text("status = 'open' AND engine LIKE 'evm_%'"))


def downgrade() -> None:
    op.drop_index("uq_paper_positions_evm_open", table_name="paper_positions")
    op.drop_table("evm_cursors")
    for ix in ("ix_evm_trades_at", "ix_evm_trades_trader_at", "ix_evm_trades_token_at"):
        op.drop_index(ix, table_name="evm_trades")
    op.drop_table("evm_trades")
    for col in ("launchpad", "creator", "created_at", "category", "last_trade_at"):
        op.drop_index(f"ix_evm_tokens_{col}", table_name="evm_tokens")
    op.drop_table("evm_tokens")
