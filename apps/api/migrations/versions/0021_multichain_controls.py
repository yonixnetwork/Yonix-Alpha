"""Multi-chain: trading controls and launchpad verification evidence.

trading_controls: operator switches below the global kill switch
(chain:<chain>, sniper, copy, new_entries, launchpad:<key>); a missing row is
the default (enabled). launchpad_checks: evidence rows (PASS / FAIL) from
on-chain verification; launchpad status is computed from them.

Revision ID: 0021
Revises: 0020
Create Date: 2026-09-29
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0021"
down_revision: Union[str, None] = "0020"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "trading_controls",
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("mode", sa.String(8), nullable=True),
        sa.Column("note", sa.String(300), nullable=True),
        sa.Column("updated_by", sa.String(64), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_table(
        "launchpad_checks",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("launchpad", sa.String(32), nullable=False),
        sa.Column("check", sa.String(32), nullable=False),
        sa.Column("status", sa.String(8), nullable=False),
        sa.Column("evidence", postgresql.JSONB(), nullable=True),
        sa.Column("source", sa.String(48), nullable=False),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_launchpad_checks_lp_check_at", "launchpad_checks", ["launchpad", "check", "checked_at"])


def downgrade() -> None:
    op.drop_index("ix_launchpad_checks_lp_check_at", table_name="launchpad_checks")
    op.drop_table("launchpad_checks")
    op.drop_table("trading_controls")
