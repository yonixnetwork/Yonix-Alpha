"""rpc providers managed from the dashboard

Revision ID: 0014
Revises: 0013
Create Date: 2026-09-27 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = '0014'
down_revision: Union[str, None] = '0013'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('rpc_providers',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('name', sa.String(length=64), nullable=False),
    sa.Column('chain', sa.String(length=16), nullable=False),
    sa.Column('provider_type', sa.String(length=32), nullable=False),
    sa.Column('rpc_url_enc', sa.String(length=2048), nullable=False),
    sa.Column('ws_url_enc', sa.String(length=2048), nullable=True),
    sa.Column('rpc_display', sa.String(length=256), nullable=False),
    sa.Column('ws_display', sa.String(length=256), nullable=True),
    sa.Column('enabled', sa.Boolean(), nullable=False),
    sa.Column('priority', sa.Integer(), nullable=False),
    sa.Column('timeout_seconds', sa.Numeric(precision=6, scale=2), nullable=False),
    sa.Column('rate_limit_rps', sa.Numeric(precision=8, scale=2), nullable=True),
    sa.Column('notes', sa.String(length=500), nullable=True),
    sa.Column('last_test', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('created_by', sa.String(length=64), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('name')
    )


def downgrade() -> None:
    op.drop_table('rpc_providers')
