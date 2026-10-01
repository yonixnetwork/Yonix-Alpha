"""Master upgrade M7: provider roles and the operator-stated plan on
rpc_providers (yonixalpha_core.provider_roles). Additive; an empty role list
means "every role", so existing providers keep their behaviour.

Revision ID: 0029
Revises: 0028
Create Date: 2026-10-01
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0029"
down_revision: Union[str, None] = "0028"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("rpc_providers", sa.Column("roles", JSONB(), nullable=False, server_default="[]"))
    op.add_column("rpc_providers", sa.Column("plan", sa.String(64), nullable=True))


def downgrade() -> None:
    op.drop_column("rpc_providers", "plan")
    op.drop_column("rpc_providers", "roles")
