"""Audit 2026-10-07 (dashboard 504s): an index for the EVM ML page's count
of copy events with a paper outcome in the window (copy_events had no index
starting with target_at, so the count read the whole table on every
refresh). Built CONCURRENTLY so writers are never blocked.

Revision ID: 0041
Revises: 0040
Create Date: 2026-10-07
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0041"
down_revision: Union[str, None] = "0040"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_copy_events_outcome_done ON copy_events (target_at) "
                   "WHERE outcome IS NOT NULL")


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_copy_events_outcome_done")
