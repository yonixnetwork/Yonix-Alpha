"""M19: ml_validation_sets (frozen validation windows no model trains on)
and ml_validation_reports (each model version scored on them), master §38.
Additive.

Revision ID: 0038
Revises: 0037
Create Date: 2026-10-05
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0038"
down_revision: Union[str, None] = "0037"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "ml_validation_sets",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("family", sa.String(32), nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("window_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("frozen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("samples", sa.Integer(), nullable=False),
        sa.Column("note", sa.String(300), nullable=True),
        sa.UniqueConstraint("family", "window_start", name="uq_ml_validation_sets_family_start"),
    )
    op.create_index("ix_ml_validation_sets_family", "ml_validation_sets", ["family"])
    op.create_table(
        "ml_validation_reports",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("set_id", sa.BigInteger(), sa.ForeignKey("ml_validation_sets.id", ondelete="CASCADE"), nullable=False),
        sa.Column("model_name", sa.String(128), nullable=False),
        sa.Column("model_version", sa.Integer(), nullable=False),
        sa.Column("evaluated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("reason", sa.String(300), nullable=True),
        sa.Column("metrics", JSONB(), nullable=False),
        sa.UniqueConstraint("set_id", "model_name", "model_version", name="uq_ml_validation_reports_set_model"),
    )
    op.create_index("ix_ml_validation_reports_set_id", "ml_validation_reports", ["set_id"])
    op.create_index("ix_ml_validation_reports_model_name", "ml_validation_reports", ["model_name"])


def downgrade() -> None:
    op.drop_index("ix_ml_validation_reports_model_name", table_name="ml_validation_reports")
    op.drop_index("ix_ml_validation_reports_set_id", table_name="ml_validation_reports")
    op.drop_table("ml_validation_reports")
    op.drop_index("ix_ml_validation_sets_family", table_name="ml_validation_sets")
    op.drop_table("ml_validation_sets")
