"""Master upgrade M3b: hourly launchpad-market regime summaries per chain
for the wallet market-regime test (yonixalpha_core.market_regimes). Additive.

Revision ID: 0026
Revises: 0025
Create Date: 2026-10-01
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0026"
down_revision: Union[str, None] = "0025"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "market_regime_hours",
        sa.Column("chain", sa.String(16), primary_key=True),
        sa.Column("hour", sa.DateTime(timezone=True), primary_key=True),
        sa.Column("volume", sa.Numeric(38, 18), nullable=False),
        sa.Column("net_flow", sa.Numeric(20, 10), nullable=True),
        sa.Column("median_range", sa.Numeric(30, 10), nullable=True),
        sa.Column("tokens", sa.Integer(), nullable=False),
        sa.Column("trades", sa.Integer(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("market_regime_hours")
