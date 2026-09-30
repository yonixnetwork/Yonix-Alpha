"""Master upgrade M1: daily launchpad activity rollup (launches, trades,
migrations, volume) for the launchpad health / 7-day activity rule. Additive.

Revision ID: 0024
Revises: 0023
Create Date: 2026-09-30
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0024"
down_revision: Union[str, None] = "0023"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "launchpad_activity",
        sa.Column("chain", sa.String(16), primary_key=True),
        sa.Column("launchpad", sa.String(32), primary_key=True),
        sa.Column("day", sa.Date(), primary_key=True),
        sa.Column("launches", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("trades", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("migrations", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("volume", sa.Numeric(78, 0), nullable=False, server_default="0"),
        sa.Column("last_launch_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_trade_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_migration_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("launchpad_activity")
