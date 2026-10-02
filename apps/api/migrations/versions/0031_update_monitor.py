"""Master upgrade M15: update_watches / update_events — the GitHub and
dependency update monitor (yonixalpha_core.update_monitor). Additive.

Revision ID: 0031
Revises: 0030
Create Date: 2026-10-02
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "0031"
down_revision: Union[str, None] = "0030"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "update_watches",
        sa.Column("key", sa.String(128), primary_key=True),
        sa.Column("category", sa.String(32), nullable=False),
        sa.Column("last_checked", sa.DateTime(timezone=True), nullable=True),
        sa.Column("latest_commit", sa.String(64), nullable=True),
        sa.Column("latest_commit_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("previous_commit", sa.String(64), nullable=True),
        sa.Column("latest_release", sa.String(128), nullable=True),
        sa.Column("previous_release", sa.String(128), nullable=True),
        sa.Column("installed_version", sa.String(64), nullable=True),
        sa.Column("classification", sa.String(24), nullable=True),
        sa.Column("flags", JSONB(), nullable=True),
        sa.Column("change_summary", JSONB(), nullable=True),
        sa.Column("error", sa.String(300), nullable=True),
    )
    op.create_table(
        "update_events",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("key", sa.String(128), nullable=False, index=True),
        sa.Column("detected_at", sa.DateTime(timezone=True), nullable=False, index=True),
        sa.Column("classification", sa.String(24), nullable=False),
        sa.Column("from_ref", sa.String(128), nullable=True),
        sa.Column("to_ref", sa.String(128), nullable=True),
        sa.Column("summary", JSONB(), nullable=True),
        sa.Column("notified", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("acknowledged_by", sa.String(64), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("update_events")
    op.drop_table("update_watches")
