"""M12c: index wallet_trade_labels by (chain, token), so the wallet-label
builder finds the next unprocessed tokens without scanning the table.
Additive.

Revision ID: 0035
Revises: 0034
Create Date: 2026-10-04
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0035"
down_revision: Union[str, None] = "0034"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index("ix_wallet_trade_labels_chain_token", "wallet_trade_labels", ["chain", "token"])


def downgrade() -> None:
    op.drop_index("ix_wallet_trade_labels_chain_token", table_name="wallet_trade_labels")
