"""live smoke tests; observation follow-up snapshots (T+5m..T+60m, migration)

Revision ID: 0013
Revises: 0012
Create Date: 2026-09-27 06:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = '0013'
down_revision: Union[str, None] = '0012'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('token_observations', sa.Column('followups', postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.create_table('live_smoke_tests',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('category', sa.String(length=16), nullable=False),
    sa.Column('max_sol', sa.Numeric(precision=38, scale=18), nullable=False),
    sa.Column('status', sa.String(length=32), nullable=False),
    sa.Column('stage', sa.String(length=48), nullable=True),
    sa.Column('stage_reason', sa.String(length=500), nullable=True),
    sa.Column('armed_by', sa.String(length=64), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('attempts', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('position_id', sa.UUID(), nullable=True),
    sa.Column('assessment_id', sa.UUID(), nullable=True),
    sa.Column('mint', sa.String(length=64), nullable=True),
    sa.Column('engine', sa.String(length=32), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['position_id'], ['paper_positions.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_live_smoke_tests_status'), 'live_smoke_tests', ['status'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_live_smoke_tests_status'), table_name='live_smoke_tests')
    op.drop_table('live_smoke_tests')
    op.drop_column('token_observations', 'followups')
