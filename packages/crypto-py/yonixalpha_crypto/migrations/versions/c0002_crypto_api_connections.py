"""crypto api_connections (encrypted exchange keys)

Revision ID: c0002
Revises: c0001
Create Date: 2026-10-16 09:00:00

Additive: one new table in the crypto schema. Downgrade drops only it.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = 'c0002'
down_revision = 'c0001'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('api_connections',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('exchange', sa.String(length=16), nullable=False),
    sa.Column('environment', sa.String(length=8), nullable=False),
    sa.Column('label', sa.String(length=48), nullable=False),
    sa.Column('key_enc', sa.Text(), nullable=False),
    sa.Column('secret_enc', sa.Text(), nullable=False),
    sa.Column('key_fingerprint', sa.String(length=64), nullable=False),
    sa.Column('key_hint', sa.String(length=12), nullable=False),
    sa.Column('enabled', sa.Boolean(), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('permissions', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('checked_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_ok_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_error', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_by', sa.String(length=64), nullable=True),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('exchange', 'environment', 'label', name='uq_api_connection_label'),
    sa.UniqueConstraint('key_fingerprint'),
    schema='crypto'
    )


def downgrade() -> None:
    op.drop_table('api_connections', schema='crypto')
