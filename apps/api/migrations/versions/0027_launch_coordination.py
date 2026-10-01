"""Master upgrade M5: launch-window coordination assessment per EVM token and
the first-funder cache of EVM wallets (yonixalpha_core.launch_coordination).
Additive.

Revision ID: 0027
Revises: 0026
Create Date: 2026-10-01
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0027"
down_revision: Union[str, None] = "0026"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("evm_tokens", sa.Column("coordination", JSONB(), nullable=True))
    op.add_column("evm_tokens", sa.Column("coordination_at", sa.DateTime(timezone=True), nullable=True))
    op.create_table(
        "evm_wallet_funders",
        sa.Column("chain", sa.String(16), primary_key=True),
        sa.Column("wallet", sa.String(42), primary_key=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("funder", sa.String(42), nullable=True),
        sa.Column("funded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("tx_hash", sa.String(66), nullable=True),
        sa.Column("detail", JSONB(), nullable=True),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_evm_wallet_funders_funder", "evm_wallet_funders", ["funder"])


def downgrade() -> None:
    op.drop_index("ix_evm_wallet_funders_funder", table_name="evm_wallet_funders")
    op.drop_table("evm_wallet_funders")
    op.drop_column("evm_tokens", "coordination_at")
    op.drop_column("evm_tokens", "coordination")
