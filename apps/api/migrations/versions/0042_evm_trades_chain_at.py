"""Server 2026-10-07 (load 9.5 on 2 vCPU, copies TOO_LATE with a 5-minute
median detection): evm_trades (4.4 M rows, 3.5 GB) had no index for "this
chain's trades since T". The copy engine reads exactly that every second per
chain (the trader is compared lower-cased, which no index serves), and so do
the 14-day prune, the market-regime windows and the wallet labels; each was a
scan of the whole table. Built CONCURRENTLY so writers are never blocked.

Revision ID: 0042
Revises: 0041
Create Date: 2026-10-07
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0042"
down_revision: Union[str, None] = "0041"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_evm_trades_chain_at ON evm_trades (chain, at)")


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_evm_trades_chain_at")
