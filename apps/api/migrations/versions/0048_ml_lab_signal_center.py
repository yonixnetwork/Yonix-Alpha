"""ML Training Lab and Signal Center: manual labels, experiments, immutable
signals and their outcomes. Four new, empty tables; nothing existing is
changed, scanned or locked. A trigger refuses every UPDATE / DELETE on
ml_signals, so a recorded prediction can never be rewritten.

Revision ID: 0048
Revises: 0047
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0048"
down_revision = "0047"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ml_manual_labels",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("opportunity_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("opportunity_outcomes.id", ondelete="SET NULL"), nullable=True),
        sa.Column("mint", sa.String(64), nullable=False),
        sa.Column("engine", sa.String(32), nullable=False),
        sa.Column("label", sa.String(40), nullable=False),
        sa.Column("value", sa.Numeric(18, 6), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("features", postgresql.JSONB(), nullable=True),
        sa.Column("feature_version", sa.String(32), nullable=True),
        sa.Column("model_version", sa.String(64), nullable=True),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("created_by", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("opportunity_id", "label", "created_by", name="uq_ml_manual_labels_opp_label_user"),
    )
    op.create_index("ix_ml_manual_labels_opportunity_id", "ml_manual_labels", ["opportunity_id"])
    op.create_index("ix_ml_manual_labels_mint", "ml_manual_labels", ["mint"])
    op.create_index("ix_ml_manual_labels_created_at", "ml_manual_labels", ["created_at"])

    op.create_table(
        "ml_experiments",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("idea", sa.Text(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("proposal", postgresql.JSONB(), nullable=False),
        sa.Column("testable", sa.Boolean(), nullable=False),
        sa.Column("created_by", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("decided_by", sa.String(64), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("status IN ('DRAFT','ACCEPTED','REJECTED','RETIRED')", name="ck_ml_experiments_status"),
    )
    op.create_index("ix_ml_experiments_status", "ml_experiments", ["status"])

    op.create_table(
        "ml_signals",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("signal_key", sa.String(160), nullable=False, unique=True),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("experiment_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("ml_experiments.id", ondelete="RESTRICT"), nullable=True),
        sa.Column("opportunity_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("mint", sa.String(64), nullable=False),
        sa.Column("engine", sa.String(32), nullable=False),
        sa.Column("category", sa.String(16), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("horizon", sa.String(8), nullable=False),
        sa.Column("prediction", postgresql.JSONB(), nullable=False),
        sa.Column("inputs", postgresql.JSONB(), nullable=False),
        sa.Column("definition", postgresql.JSONB(), nullable=False),
        sa.Column("feature_version", sa.String(32), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_ml_signals_experiment_decided_at", "ml_signals", ["experiment_id", "decided_at"])
    op.execute("""
        CREATE OR REPLACE FUNCTION ml_signals_immutable() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'ml_signals rows are immutable: a recorded prediction is never changed or deleted';
        END;
        $$ LANGUAGE plpgsql
    """)
    op.execute("CREATE TRIGGER ml_signals_immutable BEFORE UPDATE OR DELETE ON ml_signals "
               "FOR EACH ROW EXECUTE FUNCTION ml_signals_immutable()")

    op.create_table(
        "ml_signal_outcomes",
        sa.Column("signal_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("ml_signals.id", ondelete="RESTRICT"),
                  primary_key=True),
        sa.Column("evaluated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("outcome", postgresql.JSONB(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("ml_signal_outcomes")
    op.execute("DROP TRIGGER IF EXISTS ml_signals_immutable ON ml_signals")
    op.drop_index("ix_ml_signals_experiment_decided_at", table_name="ml_signals")
    op.drop_table("ml_signals")
    op.execute("DROP FUNCTION IF EXISTS ml_signals_immutable()")
    op.drop_index("ix_ml_experiments_status", table_name="ml_experiments")
    op.drop_table("ml_experiments")
    op.drop_index("ix_ml_manual_labels_created_at", table_name="ml_manual_labels")
    op.drop_index("ix_ml_manual_labels_mint", table_name="ml_manual_labels")
    op.drop_index("ix_ml_manual_labels_opportunity_id", table_name="ml_manual_labels")
    op.drop_table("ml_manual_labels")
