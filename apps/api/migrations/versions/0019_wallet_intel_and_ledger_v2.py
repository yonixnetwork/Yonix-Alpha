"""Wallet intelligence (launch_buyers) and opportunity ledger v2.

launch_buyers: the first buyers of every launch the system decided on, what
each did in its first minutes, and the launch outcome once it resolved
(outcome_resolved_at, so reputation is only ever computed from outcomes
known before a decision).

opportunity_outcomes gains the path to T+60m, theoretical vs executable
return, counterfactual / exit analysis, multi-target labels, regime tags
and shadow model scores. All nullable, observation data only.

Revision ID: 0019
Revises: 0018
Create Date: 2026-09-28
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0019"
down_revision: Union[str, None] = "0018"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

JSON_COLUMNS = ("path", "analysis", "labels", "regime", "post_exit", "ml_shadow")
NUMERIC_COLUMNS = ("theoretical_return_pct", "executable_return_pct")


def upgrade() -> None:
    op.create_table(
        "launch_buyers",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("mint", sa.String(64), nullable=False),
        sa.Column("wallet", sa.String(64), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("launch_created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("first_buy_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sol_in", sa.Numeric(20, 9), nullable=False),
        sa.Column("tokens_in", sa.Numeric(30, 0), nullable=False),
        sa.Column("sold_share_early", sa.Numeric(8, 4), nullable=True),
        sa.Column("sold_early", sa.Boolean(), nullable=True),
        sa.Column("early_window_closed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("outcome", sa.String(8), nullable=True),
        sa.Column("outcome_peak_pct", sa.Numeric(14, 4), nullable=True),
        sa.Column("outcome_drawdown_pct", sa.Numeric(14, 4), nullable=True),
        sa.Column("outcome_migrated", sa.Boolean(), nullable=True),
        sa.Column("outcome_resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("mint", "wallet", name="uq_launch_buyers_mint_wallet"),
        sa.CheckConstraint("outcome IS NULL OR outcome IN ('WIN','FLAT','LOSS')", name="ck_launch_buyers_outcome"),
    )
    for col in ("mint", "wallet", "outcome_resolved_at"):
        op.create_index(f"ix_launch_buyers_{col}", "launch_buyers", [col])
    for col in JSON_COLUMNS:
        op.add_column("opportunity_outcomes", sa.Column(col, postgresql.JSONB(), nullable=True))
    for col in NUMERIC_COLUMNS:
        op.add_column("opportunity_outcomes", sa.Column(col, sa.Numeric(14, 4), nullable=True))
    op.add_column("opportunity_outcomes", sa.Column("feature_version", sa.String(32), nullable=True))


def downgrade() -> None:
    op.drop_column("opportunity_outcomes", "feature_version")
    for col in NUMERIC_COLUMNS + JSON_COLUMNS:
        op.drop_column("opportunity_outcomes", col)
    op.drop_table("launch_buyers")
