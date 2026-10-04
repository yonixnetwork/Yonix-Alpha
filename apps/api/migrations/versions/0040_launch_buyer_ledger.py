"""M21: launch_buyers.ledger, a Solana early buyer's own sells (and later
buys) of the mint up to the launch outcome, so Solana wallet profiles get a
FIFO ledger (master §19-23). Additive, nullable.

Revision ID: 0040
Revises: 0039
Create Date: 2026-10-05
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0040"
down_revision: Union[str, None] = "0039"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("launch_buyers", sa.Column("ledger", JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("launch_buyers", "ledger")
