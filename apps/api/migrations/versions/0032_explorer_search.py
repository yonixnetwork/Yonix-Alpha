"""Master upgrade M14: Token Explorer search indexes. Name and symbol
prefix search (lower(x) LIKE 'abc%') on Solana tokens and EVM tokens uses
these expression indexes instead of a table scan. Built CONCURRENTLY so
ingestion keeps writing while they build. Additive; no data changes.

Revision ID: 0032
Revises: 0031
Create Date: 2026-10-02
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0032"
down_revision: Union[str, None] = "0031"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

INDEXES = (
    ("ix_tokens_lower_symbol", "tokens", "symbol"),
    ("ix_tokens_lower_name", "tokens", "name"),
    ("ix_evm_tokens_lower_symbol", "evm_tokens", "symbol"),
    ("ix_evm_tokens_lower_name", "evm_tokens", "name"),
)


def upgrade() -> None:
    with op.get_context().autocommit_block():
        for name, table, column in INDEXES:
            op.execute(f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {name} ON {table} (lower({column}) text_pattern_ops)")


def downgrade() -> None:
    with op.get_context().autocommit_block():
        for name, _, _ in INDEXES:
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
