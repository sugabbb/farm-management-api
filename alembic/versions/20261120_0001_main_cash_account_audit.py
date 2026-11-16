"""Support the daily main cash account audit.

Adds:

- generated ``source_table`` / ``source_id`` columns on ``main_cash_account_logs`` (derived from
  ``reference`` by PostgreSQL) plus an index, so the audit can join the log against its sources
  without parsing text per row;
- partial indexes restricting ``operational_cashflows`` / ``revenues`` to the rows that can affect
  the main account, so the audit never scans the other (millions of) transactions. They are keyed
  by ``id`` for full sweeps and by ``updated_at`` for incremental runs. The predicates use only the
  type column, so a row that *stops* qualifying (for example a top-up set back to unpaid) is still
  found by the audit;
- ``main_cash_account_audit_runs``, the history of audit executions, whose latest successful row is
  the watermark the next incremental run starts from.

Revision ID: 20261120_0001
Revises: 20261119_0001
Create Date: 2026-11-20
"""

import sqlalchemy as sa

from alembic import op
from app.db.models import SOURCE_ID_SQL, SOURCE_TABLE_SQL

revision = "20261120_0001"
down_revision = "20261119_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "main_cash_account_logs",
        sa.Column("source_table", sa.Text(), sa.Computed(SOURCE_TABLE_SQL, persisted=True), nullable=True),
    )
    op.add_column(
        "main_cash_account_logs",
        sa.Column("source_id", sa.BigInteger(), sa.Computed(SOURCE_ID_SQL, persisted=True), nullable=True),
    )
    # NOTE: the initial migration builds its tables from the ORM metadata, so on a brand new
    # database these indexes already exist by the time this revision runs, while databases created
    # before this revision do not have them. Create/drop them idempotently to serve both.
    for statement in (
        "CREATE INDEX IF NOT EXISTS ix_main_cash_account_logs_source"
        " ON main_cash_account_logs (source_table, source_id) WHERE source_table IS NOT NULL",
        "CREATE INDEX IF NOT EXISTS ix_operational_cashflows_main_topup"
        " ON operational_cashflows (id) WHERE cashflow_type = 'MAIN_ACCOUNT_TOPUP'",
        "CREATE INDEX IF NOT EXISTS ix_operational_cashflows_main_topup_updated"
        " ON operational_cashflows (updated_at, id) WHERE cashflow_type = 'MAIN_ACCOUNT_TOPUP'",
        "CREATE INDEX IF NOT EXISTS ix_revenues_main_transfer"
        " ON revenues (id) WHERE revenue_type = 'MAIN_ACCOUNT_TRANSFER'",
        "CREATE INDEX IF NOT EXISTS ix_revenues_main_transfer_updated"
        " ON revenues (updated_at, id) WHERE revenue_type = 'MAIN_ACCOUNT_TRANSFER'",
    ):
        op.execute(statement)

    op.create_table(
        "main_cash_account_audit_runs",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("mode", sa.String(length=20), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("dry_run", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("window_from", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sources_checked", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("batches", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("discrepancies", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("entries_posted", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("credit_posted", sa.Numeric(precision=18, scale=0), nullable=False, server_default="0"),
        sa.Column("debit_posted", sa.Numeric(precision=18, scale=0), nullable=False, server_default="0"),
        sa.Column("previous_amount", sa.Numeric(precision=18, scale=0), nullable=True),
        sa.Column("new_amount", sa.Numeric(precision=18, scale=0), nullable=True),
        sa.Column("log_high_id", sa.BigInteger(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.CheckConstraint("mode IN ('full','incremental')", name="mode"),
        sa.CheckConstraint("status IN ('RUNNING','SUCCESS','FAILED','SKIPPED')", name="status"),
    )
    op.create_index("ix_main_cash_account_audit_runs_mode", "main_cash_account_audit_runs", ["mode"])
    op.create_index("ix_main_cash_account_audit_runs_status", "main_cash_account_audit_runs", ["status"])
    op.create_index("ix_main_cash_account_audit_runs_started_at", "main_cash_account_audit_runs", ["started_at"])


def downgrade() -> None:
    op.drop_table("main_cash_account_audit_runs")
    for index in (
        "ix_revenues_main_transfer_updated",
        "ix_revenues_main_transfer",
        "ix_operational_cashflows_main_topup_updated",
        "ix_operational_cashflows_main_topup",
        "ix_main_cash_account_logs_source",
    ):
        op.execute(f"DROP INDEX IF EXISTS {index}")
    op.drop_column("main_cash_account_logs", "source_id")
    op.drop_column("main_cash_account_logs", "source_table")
