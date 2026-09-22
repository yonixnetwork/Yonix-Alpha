"""audit: paper_positions cost-basis CHECK constraints

A paper position with entry_price = 0 or quantity = 0 has an undefined cost
basis. Closing one raised decimal.DivisionByZero inside close_position(),
which aborted the entire manage batch — so every *other* open position's
stop-loss silently stopped being evaluated for as long as the bad row
existed, and the bad row was never cleaned up. Reachable for real:
StrategySignal.entry is Numeric(38, 18), so a genuine price below 1e-18
(ordinary for a Solana memecoin quoted per raw unit) rounds to exactly 0 on
write.

Application-level guards now reject this at entry, but the invariant
belongs in the schema too, so no future writer can reintroduce it.

Revision ID: 0008
Revises: 0007
"""
from typing import Sequence, Union

from alembic import op

revision: str = '0008'
down_revision: Union[str, None] = '0007'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Any pre-existing violating row would make the constraint creation
    # fail loudly, which is the correct outcome: such a row is corrupt and
    # an operator must decide what it should have been. There are no such
    # rows in any deployed database today (no paper position has ever been
    # opened — see docs/PAPER_TRADING.md).
    op.create_check_constraint("ck_paper_positions_entry_price_positive", "paper_positions", "entry_price > 0")
    op.create_check_constraint("ck_paper_positions_quantity_positive", "paper_positions", "quantity > 0")


def downgrade() -> None:
    op.drop_constraint("ck_paper_positions_quantity_positive", "paper_positions", type_="check")
    op.drop_constraint("ck_paper_positions_entry_price_positive", "paper_positions", type_="check")
