"""M20: research_items, the research pipeline of master §67 (RESEARCH ->
REVIEW -> PAPER -> VALIDATION -> CONTROLLED_RELEASE). Additive.

Revision ID: 0039
Revises: 0038
Create Date: 2026-10-05
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0039"
down_revision: Union[str, None] = "0038"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "research_items",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("ref", sa.String(128), nullable=True),
        sa.Column("stage", sa.String(24), nullable=False),
        sa.Column("summary", sa.String(1000), nullable=True),
        sa.Column("history", JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("source", "ref", name="uq_research_items_source_ref"),
    )
    op.create_index("ix_research_items_kind", "research_items", ["kind"])
    op.create_index("ix_research_items_stage", "research_items", ["stage"])


def downgrade() -> None:
    op.drop_index("ix_research_items_stage", table_name="research_items")
    op.drop_index("ix_research_items_kind", table_name="research_items")
    op.drop_table("research_items")
