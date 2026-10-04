"""M18: evm_exit_samples, the SELL / HOLD decision points of open EVM paper
positions with their verdicts and 15-minute outcome (master §41). Additive.

Revision ID: 0037
Revises: 0036
Create Date: 2026-10-04
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "0037"
down_revision: Union[str, None] = "0036"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "evm_exit_samples",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("position_id", UUID(as_uuid=True), nullable=False),
        sa.Column("chain", sa.String(16), nullable=False),
        sa.Column("token", sa.String(42), nullable=False),
        sa.Column("engine", sa.String(32), nullable=False),
        sa.Column("launchpad", sa.String(32), nullable=True),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("price", sa.Float(), nullable=False),
        sa.Column("features", JSONB(), nullable=False),
        sa.Column("verdicts", JSONB(), nullable=False),
        sa.Column("exit_reasons", JSONB(), nullable=False),
        sa.Column("labels", JSONB(), nullable=True),
        sa.Column("ml_shadow", JSONB(), nullable=True),
        sa.Column("feature_version", sa.String(32), nullable=False),
        sa.Column("label_version", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_evm_exit_samples_at", "evm_exit_samples", ["at"])
    op.create_index("ix_evm_exit_samples_position_at", "evm_exit_samples", ["position_id", "at"])


def downgrade() -> None:
    op.drop_index("ix_evm_exit_samples_position_at", table_name="evm_exit_samples")
    op.drop_index("ix_evm_exit_samples_at", table_name="evm_exit_samples")
    op.drop_table("evm_exit_samples")
