"""Record the nightly cash statement sync, and index what it looks up.

Adds:

- ``cash_statement_sync_runs``: one row per run of ``app.scripts.sync_cash_statements`` (the
  month it synced, status, what it changed, the month's totals, the error of a failed run). Read
  by maintainers directly in the database; there is no API for it;
- ``updated_at`` indexes on ``operational_cashflows``, ``revenues`` and their payment tables, so the
  run finds the transactions changed during its month without scanning those tables.

No rows are inserted.

Revision ID: 20261127_0001
Revises: 20261126_0001
Create Date: 2026-11-27
"""

import sqlalchemy as sa

from alembic import op

revision = "20261127_0001"
down_revision = "20261126_0001"
branch_labels = None
depends_on = None

MONEY = sa.Numeric(precision=18, scale=0)

# The initial migration builds its tables from the ORM metadata, so on a brand new database these
# indexes already exist by the time this revision runs; databases created earlier do not have
# them. Created/dropped idempotently to serve both.
UPDATED_AT_INDEXES = {
    "ix_operational_cashflows_updated_at": "operational_cashflows",
    "ix_revenues_updated_at": "revenues",
    "ix_operational_cashflow_payments_updated_at": "operational_cashflow_payments",
    "ix_revenue_payments_updated_at": "revenue_payments",
}


def upgrade() -> None:
    for index, table in UPDATED_AT_INDEXES.items():
        op.execute(f"CREATE INDEX IF NOT EXISTS {index} ON {table} (updated_at)")

    op.create_table(
        "cash_statement_sync_runs",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("changed_since", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("dry_run", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cashflows_checked", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("revenues_checked", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("adjustments_checked", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("loans_checked", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("transactions_checked", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("rows_inserted", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("rows_updated", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("rows_deleted", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("period_rows", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("period_credit", MONEY, nullable=False, server_default="0"),
        sa.Column("period_debit", MONEY, nullable=False, server_default="0"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.CheckConstraint("status IN ('RUNNING','SUCCESS','FAILED')", name="status"),
        sa.CheckConstraint("period_end >= period_start", name="period"),
    )
    op.create_index("ix_cash_statement_sync_runs_status", "cash_statement_sync_runs", ["status"])
    op.create_index("ix_cash_statement_sync_runs_started_at", "cash_statement_sync_runs", ["started_at"])
    op.create_index("ix_cash_statement_sync_runs_period", "cash_statement_sync_runs", ["period_start", "started_at"])


def downgrade() -> None:
    op.drop_table("cash_statement_sync_runs")
    for index in UPDATED_AT_INDEXES:
        op.execute(f"DROP INDEX IF EXISTS {index}")
