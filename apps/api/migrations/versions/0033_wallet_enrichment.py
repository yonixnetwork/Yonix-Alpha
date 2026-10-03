"""Master upgrade M11: wallet_enrichment - what Nansen / MadeOnSol report
about a wallet (labels, name, provider P/L) and the candidate wallets their
feeds suggest. Enrichment only. Additive.

Revision ID: 0033
Revises: 0032
Create Date: 2026-10-03
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0033"
down_revision: Union[str, None] = "0032"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "wallet_enrichment",
        sa.Column("chain", sa.String(16), primary_key=True),
        sa.Column("wallet", sa.String(64), primary_key=True),
        sa.Column("provider", sa.String(16), primary_key=True),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("data", JSONB, nullable=True),
        sa.Column("error", sa.String(300), nullable=True),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("discovered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_wallet_enrichment_fetched_at", "wallet_enrichment", ["fetched_at"])


def downgrade() -> None:
    op.drop_index("ix_wallet_enrichment_fetched_at", table_name="wallet_enrichment")
    op.drop_table("wallet_enrichment")
