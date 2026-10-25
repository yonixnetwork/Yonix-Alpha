"""Market samples: SOL/USD and Pump.fun stream activity every few minutes
(yonixalpha_core.market_samples). One new, empty table; nothing existing is
changed, scanned or locked.

Revision ID: 0050
Revises: 0049
"""

import sqlalchemy as sa
from alembic import op

revision = "0050"
down_revision = "0049"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "market_samples",
        sa.Column("at", sa.DateTime(timezone=True), primary_key=True),
        sa.Column("sol_usd", sa.Numeric(14, 4), nullable=True),
        sa.Column("sol_usd_source", sa.String(64), nullable=True),
        sa.Column("launches_last_hour", sa.Integer(), nullable=True),
        sa.Column("migrations_last_hour", sa.Integer(), nullable=True),
        sa.Column("active_tokens_5m", sa.Integer(), nullable=True),
        sa.Column("stream_heartbeat_age_s", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("market_samples")
