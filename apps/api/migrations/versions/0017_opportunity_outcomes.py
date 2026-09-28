"""opportunity_outcomes: every traded and non-traded opportunity with its
decision snapshot, forward price horizons, trade result and loss analysis

Revision ID: 0017
Revises: 0016
Create Date: 2026-09-28 13:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = '0017'
down_revision: Union[str, None] = '0016'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "opportunity_outcomes",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("key", sa.String(160), nullable=False, unique=True),
        sa.Column("mint", sa.String(64), nullable=False),
        sa.Column("symbol", sa.String(64), nullable=True),
        sa.Column("engine", sa.String(32), nullable=False),
        sa.Column("stage", sa.String(16), nullable=False),
        sa.Column("decision", sa.String(32), nullable=False),
        sa.Column("traded", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("execution_mode", sa.String(8), nullable=True),
        sa.Column("candidate_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("assessment_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("position_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("reasons", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("snapshot", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("horizons", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("peak_pct", sa.Numeric(14, 4), nullable=True),
        sa.Column("drawdown_pct", sa.Numeric(14, 4), nullable=True),
        sa.Column("migrated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("trade_result", postgresql.JSONB(), nullable=True),
        sa.Column("loss_analysis", postgresql.JSONB(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="TRACKING"),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    for col in ("mint", "engine", "decision", "traded", "candidate_id", "position_id", "decided_at", "status"):
        op.create_index(f"ix_opportunity_outcomes_{col}", "opportunity_outcomes", [col])


def downgrade() -> None:
    op.drop_table("opportunity_outcomes")
