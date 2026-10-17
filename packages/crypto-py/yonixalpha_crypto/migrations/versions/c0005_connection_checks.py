"""connection test checks

Additive: one nullable JSONB column, crypto.api_connections.checks (the last
test's separate public-data / account / order-validation results). Existing
rows keep working; the downgrade drops only this column.

Revision ID: c0005
Revises: c0004
Create Date: 2026-10-17 14:00:00
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = 'c0005'
down_revision = 'c0004'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('api_connections', sa.Column('checks', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
                  schema='crypto')


def downgrade() -> None:
    op.drop_column('api_connections', 'checks', schema='crypto')
