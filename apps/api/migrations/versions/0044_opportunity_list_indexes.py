"""Server 2026-10-08: the ML Review "Losing trades" and "Rejected, later up"
lists (/api/ml/opportunities?losses_only / rejected_up) answered 503 after
25 s: each read a week of opportunity_outcomes rows (3.1 GB table, large JSON
columns) to find the few that match. Two small partial indexes hold only the
matching rows, by decision time. The predicates match the queries exactly
(routes/ml.py renders the threshold as a literal). Built CONCURRENTLY.

Revision ID: 0044
Revises: 0043
Create Date: 2026-10-08
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0044"
down_revision: Union[str, None] = "0043"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_opportunity_outcomes_losses ON opportunity_outcomes "
                   "(decided_at) WHERE loss_analysis IS NOT NULL")
        op.execute("CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_opportunity_outcomes_rejected_up ON opportunity_outcomes "
                   "(decided_at) WHERE traded IS false AND peak_pct >= 30")


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_opportunity_outcomes_rejected_up")
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_opportunity_outcomes_losses")
