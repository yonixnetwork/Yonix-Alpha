"""Master upgrade M6: the observation of every EVM token per category
(chains/evm/observation.py), with state history and T0..T+60 snapshots.
Additive; rows are never deleted (training data).

Revision ID: 0028
Revises: 0027
Create Date: 2026-10-01
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "0028"
down_revision: Union[str, None] = "0027"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "evm_observations",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("chain", sa.String(16), nullable=False),
        sa.Column("token", sa.String(42), nullable=False),
        sa.Column("category", sa.String(16), nullable=False),
        sa.Column("state", sa.String(24), nullable=False),
        sa.Column("state_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reason", sa.String(500), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deadline", sa.DateTime(timezone=True), nullable=False),
        sa.Column("observation_reason", sa.String(200), nullable=False),
        sa.Column("expiry_reason", sa.String(64), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("extensions", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("safety_failures", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("safety_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("snapshots", JSONB(), nullable=False, server_default="{}"),
        sa.Column("history", JSONB(), nullable=False, server_default="[]"),
        sa.Column("last_decision", JSONB(), nullable=True),
        sa.UniqueConstraint("chain", "token", "category", name="uq_evm_observations_token_category"),
    )
    op.create_index("ix_evm_observations_chain_state", "evm_observations", ["chain", "state"])
    op.create_index("ix_evm_observations_started_at", "evm_observations", ["started_at"])


def downgrade() -> None:
    op.drop_index("ix_evm_observations_started_at", table_name="evm_observations")
    op.drop_index("ix_evm_observations_chain_state", table_name="evm_observations")
    op.drop_table("evm_observations")
