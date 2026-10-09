"""Server 2026-10-09: the ML Review list of a counterfactual category
("Rejection justified", "Correct rejections", "Missed winners", ...)
answered 503 after 25 s: the category is a value inside the large `analysis`
JSON, so a week of opportunity_outcomes rows (3.1 GB table) was read to find
it. Expression indexes on the category values, by decision time, hold only
the rows that have one (the predicates are implied by the list's equality
filters). Built CONCURRENTLY.

Revision ID: 0045
Revises: 0044
Create Date: 2026-10-09
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0045"
down_revision: Union[str, None] = "0044"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

CF = "(analysis -> 'counterfactual' ->> 'classification')"
EXIT = "(post_exit ->> 'classification')"


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(f"CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_opportunity_outcomes_cf_class ON opportunity_outcomes "
                   f"({CF}, decided_at) WHERE {CF} IS NOT NULL")
        op.execute(f"CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_opportunity_outcomes_exit_class ON opportunity_outcomes "
                   f"({EXIT}, decided_at) WHERE {EXIT} IS NOT NULL")
        op.execute("CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_opportunity_outcomes_recovery ON opportunity_outcomes "
                   "(decided_at) WHERE (labels ->> 'recovery') = 'true'")
        op.execute("CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_risk_assessments_engine_evaluated_at ON risk_assessments "
                   "(engine, evaluated_at)")


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_risk_assessments_engine_evaluated_at")
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_opportunity_outcomes_recovery")
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_opportunity_outcomes_exit_class")
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_opportunity_outcomes_cf_class")
