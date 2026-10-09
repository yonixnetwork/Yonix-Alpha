"""X narrative observations (shadow): one row per X lookup of a Solana mint.

Derived numbers, post IDs and links only. A new, empty table: no backfill,
nothing locked.

Revision ID: 0046
Revises: 0045
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0046"
down_revision = "0045"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "x_narrative_observations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("mint", sa.String(64), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("query", sa.String(512), nullable=True),
        sa.Column("identity_confidence", sa.Numeric(6, 4), nullable=True),
        sa.Column("narrative_score", sa.Numeric(8, 2), nullable=True),
        sa.Column("social_data_quality", sa.String(32), nullable=True),
        sa.Column("onchain_score", sa.Numeric(12, 4), nullable=True),
        sa.Column("combined_score", sa.Numeric(12, 4), nullable=True),
        sa.Column("posts_returned", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("features", postgresql.JSONB(), nullable=True),
        sa.Column("evidence", postgresql.JSONB(), nullable=True),
        sa.Column("decision_context", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_x_narrative_observations_mint_observed_at", "x_narrative_observations", ["mint", "observed_at"])


def downgrade() -> None:
    op.drop_index("ix_x_narrative_observations_mint_observed_at", table_name="x_narrative_observations")
    op.drop_table("x_narrative_observations")
