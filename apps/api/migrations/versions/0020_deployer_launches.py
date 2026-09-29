"""Deployer (creator) launch history for time-aware deployer intelligence.

deployer_launches: one row per launch this system observed, with its
creator, creation time, and the outcome once it resolved (resolved_at). A
deployer's features at time T read only launches created before T whose
outcome resolved at or before T, so no later launch leaks into an earlier
decision. Observation data only; nothing here trades.

Revision ID: 0020
Revises: 0019
Create Date: 2026-09-29
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0020"
down_revision: Union[str, None] = "0019"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "deployer_launches",
        sa.Column("mint", sa.String(64), primary_key=True),
        sa.Column("creator", sa.String(64), nullable=False),
        sa.Column("launch_created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("migrated", sa.Boolean(), nullable=True),
        sa.Column("time_to_migration_seconds", sa.Integer(), nullable=True),
        sa.Column("peak_mc_sol", sa.Numeric(20, 4), nullable=True),
        sa.Column("outcome", sa.String(8), nullable=True),
        sa.Column("creator_sold_early", sa.Boolean(), nullable=True),
        sa.Column("creator_sell_share", sa.Numeric(8, 4), nullable=True),
        sa.Column("volume_sol_60m", sa.Numeric(20, 4), nullable=True),
        sa.Column("unique_buyers_60m", sa.Integer(), nullable=True),
        sa.Column("tracked_seconds", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("outcome IS NULL OR outcome IN ('WIN','FLAT','LOSS')", name="ck_deployer_launches_outcome"),
    )
    op.create_index("ix_deployer_launches_creator_created", "deployer_launches", ["creator", "launch_created_at"])
    op.create_index("ix_deployer_launches_resolved_at", "deployer_launches", ["resolved_at"])


def downgrade() -> None:
    op.drop_index("ix_deployer_launches_resolved_at", table_name="deployer_launches")
    op.drop_index("ix_deployer_launches_creator_created", table_name="deployer_launches")
    op.drop_table("deployer_launches")
