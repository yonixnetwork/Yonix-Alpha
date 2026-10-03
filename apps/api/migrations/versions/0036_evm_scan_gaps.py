"""M16: evm_scan_gaps, the block ranges a live EVM scan skipped (lag over
max_lag_minutes), recorded and backfilled instead of lost. Additive.

Revision ID: 0036
Revises: 0035
Create Date: 2026-10-04
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0036"
down_revision: Union[str, None] = "0035"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "evm_scan_gaps",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("chain", sa.String(16), nullable=False),
        sa.Column("launchpad", sa.String(32), nullable=False),
        sa.Column("from_block", sa.BigInteger(), nullable=False),
        sa.Column("to_block", sa.BigInteger(), nullable=False),
        sa.Column("next_block", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(12), nullable=False),
        sa.Column("detected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reason", JSONB(), nullable=False),
        sa.Column("launches", sa.Integer(), nullable=False),
        sa.Column("trades", sa.Integer(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.String(300), nullable=True),
    )
    op.create_index("ix_evm_scan_gaps_status", "evm_scan_gaps", ["status"])
    op.create_index("ix_evm_scan_gaps_chain_launchpad_status", "evm_scan_gaps", ["chain", "launchpad", "status"])


def downgrade() -> None:
    op.drop_index("ix_evm_scan_gaps_chain_launchpad_status", table_name="evm_scan_gaps")
    op.drop_index("ix_evm_scan_gaps_status", table_name="evm_scan_gaps")
    op.drop_table("evm_scan_gaps")
