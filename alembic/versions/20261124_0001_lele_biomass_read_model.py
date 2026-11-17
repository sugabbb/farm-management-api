"""Create the Lele biomass read model and its rebuild queue.

- ``lele_biomass_jobs``: groups queued for a background rebuild (transactional outbox);
- ``lele_biomass_snapshots``: one row per Lele group (details, latest sampling, harvest prediction);
- ``lele_biomass_chart_dates`` / ``lele_biomass_chart_series`` / ``lele_biomass_chart_values``: the
  group's weight chart.

No rows are inserted here: the read model of existing groups is built by the manual backfill
command (``python -m app.scripts.backfill_lele_sampling_results``); from then on every change
rebuilds its group in the background.

Revision ID: 20261124_0001
Revises: 20261123_0001
Create Date: 2026-11-24
"""

import sqlalchemy as sa

from alembic import op

revision = "20261124_0001"
down_revision = "20261123_0001"
branch_labels = None
depends_on = None

WEIGHT = sa.Numeric(precision=12, scale=3)
BIOMASS = sa.Numeric(precision=18, scale=3)


def upgrade() -> None:
    op.create_table(
        "lele_biomass_jobs",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "group_id", sa.BigInteger(), sa.ForeignKey("livestock_groups.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("reason", sa.String(length=60), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_lele_biomass_jobs_group_id", "lele_biomass_jobs", ["group_id"])

    op.create_table(
        "lele_biomass_snapshots",
        sa.Column(
            "group_id",
            sa.BigInteger(),
            sa.ForeignKey("livestock_groups.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("location", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("closed_at", sa.Date(), nullable=True),
        sa.Column("initial_population", sa.BigInteger(), nullable=False),
        sa.Column("current_population", sa.BigInteger(), nullable=False),
        sa.Column("population_as_of", sa.Date(), nullable=False),
        sa.Column("initial_weight_g", WEIGHT, nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("target_weight_g", WEIGHT, nullable=False),
        sa.Column("default_adg_g", WEIGHT, nullable=False),
        sa.Column("latest_sampling_date", sa.Date(), nullable=True),
        sa.Column("latest_weight_g", WEIGHT, nullable=True),
        sa.Column("latest_population", sa.BigInteger(), nullable=True),
        sa.Column("latest_biomass_kg", BIOMASS, nullable=True),
        sa.Column("baseline_source", sa.String(length=10), nullable=True),
        sa.Column("baseline_date", sa.Date(), nullable=True),
        sa.Column("baseline_weight_g", WEIGHT, nullable=True),
        sa.Column("adg_source", sa.String(length=10), nullable=True),
        sa.Column("adg_g", WEIGHT, nullable=True),
        sa.Column("days_to_target", sa.Integer(), nullable=True),
        sa.Column("harvest_date", sa.Date(), nullable=True),
        sa.Column("ready_to_sell", sa.Boolean(), nullable=True),
        sa.Column("remaining_weight_g", WEIGHT, nullable=True),
        sa.Column("projected_biomass_kg", BIOMASS, nullable=True),
        sa.Column("chart_end_date", sa.Date(), nullable=False),
        sa.Column("y_max_g", WEIGHT, nullable=False),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("baseline_source IS NULL OR baseline_source IN ('SAMPLING','STOCKING')", name="baseline"),
        sa.CheckConstraint("adg_source IS NULL OR adg_source IN ('ACTUAL','DEFAULT')", name="adg_source"),
        sa.CheckConstraint(
            "(baseline_source IS NULL AND baseline_date IS NULL AND baseline_weight_g IS NULL AND adg_source IS NULL"
            " AND adg_g IS NULL AND days_to_target IS NULL AND harvest_date IS NULL AND ready_to_sell IS NULL"
            " AND remaining_weight_g IS NULL AND projected_biomass_kg IS NULL)"
            " OR (baseline_source IS NOT NULL AND baseline_date IS NOT NULL AND baseline_weight_g IS NOT NULL"
            " AND adg_source IS NOT NULL AND adg_g IS NOT NULL AND days_to_target IS NOT NULL"
            " AND harvest_date IS NOT NULL AND ready_to_sell IS NOT NULL AND remaining_weight_g IS NOT NULL"
            " AND projected_biomass_kg IS NOT NULL)",
            name="projection",
        ),
    )

    op.create_table(
        "lele_biomass_chart_dates",
        sa.Column(
            "group_id",
            sa.BigInteger(),
            sa.ForeignKey("lele_biomass_snapshots.group_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("chart_date", sa.Date(), primary_key=True),
        sa.Column("day", sa.Integer(), nullable=False),
        sa.Column("week", sa.Integer(), nullable=True),
        sa.Column("is_sampling", sa.Boolean(), nullable=False),
        sa.Column("is_harvest_date", sa.Boolean(), nullable=False),
        sa.Column("actual_weight_g", WEIGHT, nullable=True),
        sa.Column("population", sa.BigInteger(), nullable=True),
        sa.Column("biomass_kg", BIOMASS, nullable=True),
    )

    op.create_table(
        "lele_biomass_chart_series",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "group_id",
            sa.BigInteger(),
            sa.ForeignKey("lele_biomass_snapshots.group_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("position", sa.SmallInteger(), nullable=False),
        sa.Column("name", sa.String(length=60), nullable=False),
        sa.Column("type", sa.String(length=10), nullable=False),
        sa.Column("is_latest", sa.Boolean(), nullable=False),
        sa.Column("adg_g", WEIGHT, nullable=False),
        sa.Column("start_date", sa.Date(), nullable=True),
        sa.Column("start_weight_g", WEIGHT, nullable=True),
        sa.Column("days_to_target", sa.Integer(), nullable=True),
        sa.Column("projected_sell_date", sa.Date(), nullable=True),
        sa.Column("period_from", sa.String(length=10), nullable=True),
        sa.Column("period_from_date", sa.Date(), nullable=True),
        sa.Column("period_to_date", sa.Date(), nullable=True),
        sa.Column("period_days", sa.Integer(), nullable=True),
        sa.Column("period_from_weight_g", WEIGHT, nullable=True),
        sa.Column("period_to_weight_g", WEIGHT, nullable=True),
        sa.Column("period_weight_gain_g", WEIGHT, nullable=True),
        sa.UniqueConstraint("group_id", "position"),
        sa.CheckConstraint("type IN ('DEFAULT','REAL')", name="type"),
        sa.CheckConstraint("period_from IS NULL OR period_from IN ('SAMPLING','STOCKING')", name="period_from"),
    )
    op.create_index("ix_lele_biomass_chart_series_group_id", "lele_biomass_chart_series", ["group_id"])

    op.create_table(
        "lele_biomass_chart_values",
        sa.Column(
            "series_id",
            sa.BigInteger(),
            sa.ForeignKey("lele_biomass_chart_series.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("chart_date", sa.Date(), primary_key=True),
        sa.Column("weight_g", WEIGHT, nullable=False),
    )


def downgrade() -> None:
    op.drop_table("lele_biomass_chart_values")
    op.drop_table("lele_biomass_chart_series")
    op.drop_table("lele_biomass_chart_dates")
    op.drop_table("lele_biomass_snapshots")
    op.drop_table("lele_biomass_jobs")
