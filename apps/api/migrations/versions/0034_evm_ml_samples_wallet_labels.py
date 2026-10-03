"""Master upgrade M12: evm_ml_samples (EVM opportunities as ML samples with
BUY / WAIT / REJECT verdicts) and wallet_trade_labels (how wallets trade:
SUCCESSFUL / FAILED / LATE entries, PREMATURE / LATE exits, missed winners).
Review data for shadow models. Additive.

Revision ID: 0034
Revises: 0033
Create Date: 2026-10-04
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "0034"
down_revision: Union[str, None] = "0033"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "evm_ml_samples",
        sa.Column("chain", sa.String(16), primary_key=True),
        sa.Column("token", sa.String(42), primary_key=True),
        sa.Column("category", sa.String(16), primary_key=True),
        sa.Column("launchpad", sa.String(32), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("features", JSONB, nullable=False),
        sa.Column("labels", JSONB, nullable=False),
        sa.Column("feature_version", sa.String(32), nullable=False),
        sa.Column("label_version", sa.String(32), nullable=False),
        sa.Column("verdicts", JSONB, nullable=False),
        sa.Column("observation_state", sa.String(24), nullable=False),
        sa.Column("traded", sa.Boolean(), nullable=False),
        sa.Column("position_id", UUID(as_uuid=True), nullable=True),
        sa.Column("executable_return_pct", sa.Float(), nullable=True),
        sa.Column("ml_shadow", JSONB, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_evm_ml_samples_decided_at", "evm_ml_samples", ["decided_at"])
    op.create_table(
        "wallet_trade_labels",
        sa.Column("chain", sa.String(16), primary_key=True),
        sa.Column("wallet", sa.String(64), primary_key=True),
        sa.Column("token", sa.String(42), primary_key=True),
        sa.Column("kind", sa.String(8), nullable=False),
        sa.Column("launchpad", sa.String(32), nullable=False),
        sa.Column("entry_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("labels", JSONB, nullable=False),
        sa.Column("outcome", JSONB, nullable=False),
        sa.Column("features", JSONB, nullable=False),
        sa.Column("feature_version", sa.String(32), nullable=False),
        sa.Column("label_version", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_wallet_trade_labels_entry_at", "wallet_trade_labels", ["entry_at"])


def downgrade() -> None:
    op.drop_index("ix_wallet_trade_labels_entry_at", table_name="wallet_trade_labels")
    op.drop_table("wallet_trade_labels")
    op.drop_index("ix_evm_ml_samples_decided_at", table_name="evm_ml_samples")
    op.drop_table("evm_ml_samples")
