"""Monthly cut-off: the month-end close aggregated from cash_statements.

Adds ``monthly_cutoffs`` (one immutable snapshot per run, ``is_latest`` marking the one reported
for its month) and its details: ``monthly_cutoff_categories`` (the month's statement grouped by
category), ``monthly_cutoff_profit_loss`` (P&L lines), ``monthly_cutoff_loans`` (loans at month
end) and ``monthly_cutoff_receivables`` (what customers owed at month end, with aging). Written by
``app.scripts.monthly_cutoff``; no rows are inserted here.

Revision ID: 20261128_0001
Revises: 20261127_0001
Create Date: 2026-11-28
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "20261128_0001"
down_revision = "20261127_0001"
branch_labels = None
depends_on = None

MONEY = sa.Numeric(precision=18, scale=0)


def money(name: str) -> sa.Column:
    return sa.Column(name, MONEY, nullable=False, server_default="0")


def cutoff_id() -> sa.Column:
    return sa.Column(
        "cutoff_id", sa.BigInteger(), sa.ForeignKey("monthly_cutoffs.id", ondelete="CASCADE"), nullable=False
    )


def upgrade() -> None:
    op.create_table(
        "monthly_cutoffs",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("is_latest", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "sync_run_id",
            sa.BigInteger(),
            sa.ForeignKey("cash_statement_sync_runs.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("warnings", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        money("cash_opening"),
        money("opening_balances_in_period"),
        money("cash_in"),
        money("cash_out"),
        money("cash_closing"),
        money("opening_balances_total"),
        sa.Column("statement_rows", sa.Integer(), nullable=False, server_default="0"),
        money("revenue"),
        money("operating_expenses"),
        money("other_expenses"),
        money("finance_costs"),
        money("net_profit"),
        sa.Column("loans_outstanding", sa.Integer(), nullable=False, server_default="0"),
        money("loan_principal_outstanding"),
        money("loan_interest_outstanding"),
        money("loan_overdue_amount"),
        money("loan_principal_repaid"),
        money("loan_interest_repaid"),
        sa.Column("receivable_invoices", sa.Integer(), nullable=False, server_default="0"),
        money("receivables"),
        money("receivables_pending_verification"),
        sa.CheckConstraint("status IN ('RUNNING','SUCCESS','FAILED')", name="status"),
        sa.CheckConstraint("period_end >= period_start", name="period"),
        sa.CheckConstraint("NOT is_latest OR status = 'SUCCESS'", name="latest"),
    )
    op.create_index("ix_monthly_cutoffs_status", "monthly_cutoffs", ["status"])
    op.create_index("ix_monthly_cutoffs_sync_run_id", "monthly_cutoffs", ["sync_run_id"])
    op.create_index("ix_monthly_cutoffs_period", "monthly_cutoffs", ["period_start", "started_at"])
    op.create_index(
        "uq_monthly_cutoffs_latest",
        "monthly_cutoffs",
        ["period_start"],
        unique=True,
        postgresql_where=sa.text("is_latest"),
    )

    op.create_table(
        "monthly_cutoff_categories",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        cutoff_id(),
        sa.Column("direction", sa.String(length=10), nullable=False),
        sa.Column("source_table", sa.String(length=40), nullable=False),
        sa.Column("category", sa.String(length=40), nullable=False),
        sa.Column("rows", sa.Integer(), nullable=False),
        sa.Column("amount", MONEY, nullable=False),
        sa.CheckConstraint("direction IN ('credit','debit')", name="direction"),
        sa.UniqueConstraint("cutoff_id", "direction", "source_table", "category"),
    )

    op.create_table(
        "monthly_cutoff_profit_loss",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        cutoff_id(),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("section", sa.String(length=20), nullable=False),
        sa.Column("line", sa.String(length=40), nullable=False),
        sa.Column("amount", MONEY, nullable=False),
        sa.CheckConstraint(
            "section IN ('REVENUE','OPERATING_EXPENSE','OTHER_EXPENSE','FINANCE_COST','NON_PL')", name="section"
        ),
        sa.UniqueConstraint("cutoff_id", "section", "line"),
    )

    op.create_table(
        "monthly_cutoff_loans",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        cutoff_id(),
        sa.Column("loan_id", sa.BigInteger(), nullable=False),
        sa.Column("lender_id", sa.BigInteger(), nullable=False),
        sa.Column("lender_name", sa.String(length=255), nullable=False),
        sa.Column("reference_number", sa.String(length=100), nullable=True),
        sa.Column("loan_type", sa.String(length=20), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("maturity_date", sa.Date(), nullable=False),
        sa.Column("principal_amount", MONEY, nullable=False),
        sa.Column("principal_paid", MONEY, nullable=False),
        sa.Column("principal_outstanding", MONEY, nullable=False),
        sa.Column("interest_total", MONEY, nullable=False),
        sa.Column("interest_paid", MONEY, nullable=False),
        sa.Column("interest_outstanding", MONEY, nullable=False),
        sa.Column("overdue_installments", sa.Integer(), nullable=False),
        sa.Column("overdue_amount", MONEY, nullable=False),
        sa.Column("principal_repaid_in_period", MONEY, nullable=False),
        sa.Column("interest_repaid_in_period", MONEY, nullable=False),
        sa.UniqueConstraint("cutoff_id", "loan_id"),
    )

    op.create_table(
        "monthly_cutoff_receivables",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        cutoff_id(),
        sa.Column("customer_id", sa.BigInteger(), nullable=True),
        sa.Column("customer_name", sa.String(length=255), nullable=False),
        sa.Column("invoices", sa.Integer(), nullable=False),
        sa.Column("outstanding", MONEY, nullable=False),
        sa.Column("aging_0_30", MONEY, nullable=False),
        sa.Column("aging_31_60", MONEY, nullable=False),
        sa.Column("aging_61_90", MONEY, nullable=False),
        sa.Column("aging_over_90", MONEY, nullable=False),
        sa.Column("pending_verification", MONEY, nullable=False),
    )
    op.create_index("ix_monthly_cutoff_receivables_cutoff_id", "monthly_cutoff_receivables", ["cutoff_id"])


def downgrade() -> None:
    for table in (
        "monthly_cutoff_receivables",
        "monthly_cutoff_loans",
        "monthly_cutoff_profit_loss",
        "monthly_cutoff_categories",
        "monthly_cutoffs",
    ):
        op.drop_table(table)
