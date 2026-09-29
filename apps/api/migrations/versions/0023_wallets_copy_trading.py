"""Multi-chain P4: wallet profiles and copy trading (targets, events, copy
positions) plus a guard against duplicate open copy positions. Additive.

Revision ID: 0023
Revises: 0022
Create Date: 2026-09-29
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0023"
down_revision: Union[str, None] = "0022"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "wallet_profiles",
        sa.Column("chain", sa.String(16), primary_key=True),
        sa.Column("wallet", sa.String(64), primary_key=True),
        sa.Column("metrics", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("labels", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("score", sa.Numeric(8, 4), nullable=True),
        sa.Column("score_detail", postgresql.JSONB(), nullable=True),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("trades", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("first_seen", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_seen", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_wallet_profiles_last_seen", "wallet_profiles", ["last_seen"])
    op.create_table(
        "copy_targets",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("chain", sa.String(16), nullable=False),
        sa.Column("wallet", sa.String(64), nullable=False),
        sa.Column("label", sa.String(64), nullable=True),
        sa.Column("mode", sa.String(16), nullable=False, server_default="NOTIFY"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("settings", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("created_by", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("chain", "wallet", name="uq_copy_targets_chain_wallet"),
    )
    op.create_index("ix_copy_targets_chain", "copy_targets", ["chain"])
    op.create_table(
        "copy_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("target_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("copy_targets.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("chain", sa.String(16), nullable=False),
        sa.Column("wallet", sa.String(64), nullable=False),
        sa.Column("token", sa.String(64), nullable=False),
        sa.Column("side", sa.String(4), nullable=False),
        sa.Column("source_event_id", sa.String(128), nullable=False),
        sa.Column("target_token_amount", sa.Numeric(78, 0), nullable=False),
        sa.Column("target_quote_amount", sa.Numeric(78, 0), nullable=False),
        sa.Column("target_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("detected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decision", sa.String(12), nullable=False),
        sa.Column("reason", sa.String(300), nullable=True),
        sa.Column("detail", postgresql.JSONB(), nullable=True),
        sa.Column("latency_ms", postgresql.JSONB(), nullable=True),
        sa.Column("position_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("paper_positions.id", ondelete="SET NULL"),
                  nullable=True),
        sa.UniqueConstraint("target_id", "source_event_id", name="uq_copy_events_target_source"),
    )
    op.create_index("ix_copy_events_target_at", "copy_events", ["target_id", "detected_at"])
    op.create_index("ix_copy_events_token", "copy_events", ["token"])
    op.create_table(
        "copy_positions",
        sa.Column("position_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("paper_positions.id", ondelete="CASCADE"),
                  primary_key=True),
        sa.Column("target_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("copy_targets.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("chain", sa.String(16), nullable=False),
        sa.Column("token", sa.String(64), nullable=False),
        sa.Column("target_tokens", sa.Numeric(78, 0), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_copy_positions_target_id", "copy_positions", ["target_id"])
    op.create_index("uq_paper_positions_copy_open", "paper_positions", ["engine", "asset_id"], unique=True,
                    postgresql_where=sa.text("status = 'open' AND engine LIKE 'copy_%'"))


def downgrade() -> None:
    op.drop_index("uq_paper_positions_copy_open", table_name="paper_positions")
    op.drop_index("ix_copy_positions_target_id", table_name="copy_positions")
    op.drop_table("copy_positions")
    op.drop_index("ix_copy_events_token", table_name="copy_events")
    op.drop_index("ix_copy_events_target_at", table_name="copy_events")
    op.drop_table("copy_events")
    op.drop_index("ix_copy_targets_chain", table_name="copy_targets")
    op.drop_table("copy_targets")
    op.drop_index("ix_wallet_profiles_last_seen", table_name="wallet_profiles")
    op.drop_table("wallet_profiles")
