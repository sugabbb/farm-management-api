"""Create lele_sampling_results (per-fish weight and real ADG of every Lele sampling).

No rows are inserted here: results of samplings that already exist are computed by the manual
backfill command (``python -m app.scripts.backfill_lele_sampling_results``); from then on every
sampling create/update keeps them up to date.

Revision ID: 20261122_0001
Revises: 20261121_0001
Create Date: 2026-11-22
"""

import sqlalchemy as sa

from alembic import op

revision = "20261122_0001"
down_revision = "20261121_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "lele_sampling_results",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "sampling_id",
            sa.BigInteger(),
            sa.ForeignKey("lele_samplings.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("fish_count", sa.Integer(), nullable=False),
        sa.Column("total_weight_kg", sa.Numeric(precision=14, scale=3), nullable=False),
        sa.Column("average_weight_g", sa.Numeric(precision=12, scale=3), nullable=False),
        sa.Column(
            "previous_sampling_id",
            sa.BigInteger(),
            sa.ForeignKey("lele_samplings.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("days_since_previous", sa.Integer(), nullable=True),
        sa.Column("weight_gain_g", sa.Numeric(precision=12, scale=3), nullable=True),
        sa.Column("adg_g", sa.Numeric(precision=12, scale=3), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("sampling_id"),
        sa.CheckConstraint("fish_count > 0 AND total_weight_kg > 0 AND average_weight_g > 0", name="weights"),
        sa.CheckConstraint(
            "(days_since_previous IS NULL AND weight_gain_g IS NULL AND adg_g IS NULL)"
            " OR (days_since_previous > 0 AND weight_gain_g IS NOT NULL AND adg_g IS NOT NULL)",
            name="interval",
        ),
    )
    op.create_index("ix_lele_sampling_results_previous_sampling_id", "lele_sampling_results", ["previous_sampling_id"])


def downgrade() -> None:
    op.drop_table("lele_sampling_results")
