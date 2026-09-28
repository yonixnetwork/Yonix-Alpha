"""Rent-reclaim orders: execution_orders.side RENT (closes the wallet's empty
token accounts so their rent deposit returns) and status SKIPPED (nothing
to close, nothing sent).

Revision ID: 0018
Revises: 0017
Create Date: 2026-09-28
"""

from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("ck_execution_orders_side", "execution_orders", type_="check")
    op.create_check_constraint("ck_execution_orders_side", "execution_orders", "side IN ('BUY','SELL','RENT')")
    op.drop_constraint("ck_execution_orders_status", "execution_orders", type_="check")
    op.create_check_constraint(
        "ck_execution_orders_status", "execution_orders",
        "status IN ('PENDING','SIGNED','SUBMITTED','CONFIRMED','FAILED','EXPIRED','CANCELLED','SKIPPED')")


def downgrade() -> None:
    op.execute("DELETE FROM execution_orders WHERE side = 'RENT'")
    op.drop_constraint("ck_execution_orders_status", "execution_orders", type_="check")
    op.create_check_constraint(
        "ck_execution_orders_status", "execution_orders",
        "status IN ('PENDING','SIGNED','SUBMITTED','CONFIRMED','FAILED','EXPIRED','CANCELLED')")
    op.drop_constraint("ck_execution_orders_side", "execution_orders", type_="check")
    op.create_check_constraint("ck_execution_orders_side", "execution_orders", "side IN ('BUY','SELL')")
