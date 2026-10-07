"""Server 2026-10-07: data-evm's entry pass ("tokens with a fresh safety
check, newest trade first") ran 49 s per pass: evm_tokens (238 k rows,
692 MB) had no index on safety_at, so the planner walked last_trade_at over
the whole table. Built CONCURRENTLY so writers are never blocked.

Revision ID: 0043
Revises: 0042
Create Date: 2026-10-07
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0043"
down_revision: Union[str, None] = "0042"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_evm_tokens_chain_safety_at ON evm_tokens (chain, safety_at)")


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_evm_tokens_chain_safety_at")
