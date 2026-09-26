"""token observations: outcome and context of every fresh pump.fun observation window

Revision ID: 0012
Revises: 0011
Create Date: 2026-09-26 06:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = '0012'
down_revision: Union[str, None] = '0011'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('token_observations',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('mint', sa.String(length=64), nullable=False),
    sa.Column('symbol', sa.String(length=32), nullable=True),
    sa.Column('name', sa.String(length=128), nullable=True),
    sa.Column('creator', sa.String(length=64), nullable=True),
    sa.Column('launched_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('outcome', sa.String(length=24), nullable=False),
    sa.Column('trend', sa.String(length=16), nullable=True),
    sa.Column('reasons', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('report', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('candidate_id', sa.UUID(), nullable=True),
    sa.Column('decided_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('mint')
    )
    op.create_index(op.f('ix_token_observations_outcome'), 'token_observations', ['outcome'], unique=False)
    op.create_index(op.f('ix_token_observations_decided_at'), 'token_observations', ['decided_at'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_token_observations_decided_at'), table_name='token_observations')
    op.drop_index(op.f('ix_token_observations_outcome'), table_name='token_observations')
    op.drop_table('token_observations')
