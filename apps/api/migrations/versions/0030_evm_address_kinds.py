"""Master upgrade M10b: evm_address_kinds — whether an address seen as a
trader is a wallet, an EIP-7702 delegated wallet or a contract
(yonixalpha_core.chains.evm.address_kinds). Additive.

Revision ID: 0030
Revises: 0029
Create Date: 2026-10-02
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0030"
down_revision: Union[str, None] = "0029"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "evm_address_kinds",
        sa.Column("chain", sa.String(16), primary_key=True),
        sa.Column("address", sa.String(42), primary_key=True),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("delegate", sa.String(42), nullable=True),
        sa.Column("code_size", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("evm_address_kinds")
