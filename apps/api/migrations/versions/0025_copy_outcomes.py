"""Master upgrade M4b: paper copy outcome per copy event (would-have-won /
would-have-lost / missed; yonixalpha_core.copy_outcomes). Additive.

Revision ID: 0025
Revises: 0024
Create Date: 2026-10-01
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0025"
down_revision: Union[str, None] = "0024"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("copy_events", sa.Column("outcome", postgresql.JSONB(), nullable=True))
    op.add_column("copy_events", sa.Column("outcome_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_copy_events_outcome_pending", "copy_events", ["target_at"],
                    postgresql_where=sa.text("outcome_at IS NULL AND side = 'BUY'"))


def downgrade() -> None:
    op.drop_index("ix_copy_events_outcome_pending", table_name="copy_events")
    op.drop_column("copy_events", "outcome_at")
    op.drop_column("copy_events", "outcome")
