"""execution_orders.diagnostics: decision context, stage timings and the
price-execution analysis of each LIVE order

Revision ID: 0016
Revises: 0015
Create Date: 2026-09-28 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = '0016'
down_revision: Union[str, None] = '0015'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("execution_orders", sa.Column("diagnostics", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("execution_orders", "diagnostics")
