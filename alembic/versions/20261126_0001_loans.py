"""Loan management: lenders, loans, their installment schedule and their repayments.

Short-term loans have a single installment with a flat interest amount; long-term loans have
monthly installments with an annual interest rate (see ``app.features.loan_service``).

Also makes loan repayments a source of the main cash account and of the cash statements:

- ``main_cash_account_events`` accepts ``loan_repayments`` movements;
- the generated ``source_table`` / ``source_id`` columns of ``main_cash_account_logs`` recognise
  ``loan_repayments:<id>:<action>`` references, so the daily audit can reconcile them. PostgreSQL
  cannot change the expression of a generated column in place, so both columns (and their index)
  are recreated, which rewrites the log table once;
- ``cash_statements`` accepts rows whose source is ``loans``.

No rows are inserted: loans are entered through the API.

Revision ID: 20261126_0001
Revises: 20261125_0001
Create Date: 2026-11-26
"""

import sqlalchemy as sa

from alembic import op

revision = "20261126_0001"
down_revision = "20261125_0001"
branch_labels = None
depends_on = None

MONEY = sa.Numeric(precision=18, scale=0)

# Kept literal (not imported from app.db.models) so replaying the history never changes.
OLD_PATTERN = "^(operational_cashflows|revenues):[0-9]+:"
NEW_PATTERN = "^(operational_cashflows|revenues|loan_repayments):[0-9]+:"


def source_table_sql(pattern: str) -> str:
    return f"CASE WHEN reference ~ '{pattern}' THEN split_part(reference, ':', 1) END"


def source_id_sql(pattern: str) -> str:
    return f"CASE WHEN reference ~ '{pattern}' THEN split_part(reference, ':', 2)::bigint END"


def recreate_log_source_columns(pattern: str) -> None:
    op.execute("DROP INDEX IF EXISTS ix_main_cash_account_logs_source")
    op.drop_column("main_cash_account_logs", "source_id")
    op.drop_column("main_cash_account_logs", "source_table")
    op.add_column(
        "main_cash_account_logs",
        sa.Column("source_table", sa.Text(), sa.Computed(source_table_sql(pattern), persisted=True), nullable=True),
    )
    op.add_column(
        "main_cash_account_logs",
        sa.Column("source_id", sa.BigInteger(), sa.Computed(source_id_sql(pattern), persisted=True), nullable=True),
    )
    op.execute(
        "CREATE INDEX ix_main_cash_account_logs_source"
        " ON main_cash_account_logs (source_table, source_id) WHERE source_table IS NOT NULL"
    )


def replace_check(table: str, name: str, condition: str) -> None:
    op.execute(f"ALTER TABLE {table} DROP CONSTRAINT {name}")
    op.execute(f"ALTER TABLE {table} ADD CONSTRAINT {name} CHECK ({condition})")


def upgrade() -> None:
    op.create_table(
        "lenders",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("contact_person", sa.String(length=255), nullable=True),
        sa.Column("phone", sa.String(length=50), nullable=True),
        sa.Column("email", sa.String(length=255), nullable=True),
        sa.Column("address", sa.Text(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("uq_lenders_name_lower", "lenders", [sa.text("lower(name)")], unique=True)
    op.create_index("ix_lenders_is_active", "lenders", ["is_active"])

    op.create_table(
        "loans",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("lender_id", sa.BigInteger(), sa.ForeignKey("lenders.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("reference_number", sa.String(length=100), nullable=True),
        sa.Column("loan_type", sa.String(length=20), nullable=False),
        sa.Column("principal_amount", MONEY, nullable=False),
        sa.Column("interest_amount", MONEY, nullable=True),
        sa.Column("annual_interest_rate", sa.Numeric(precision=7, scale=4), nullable=True),
        sa.Column("interest_method", sa.String(length=10), nullable=True),
        sa.Column("term_months", sa.Integer(), nullable=True),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("first_due_date", sa.Date(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_by", sa.BigInteger(), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_by", sa.BigInteger(), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=True),
        sa.Column("cancel_reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("loan_type IN ('SHORT_TERM','LONG_TERM')", name="type"),
        sa.CheckConstraint(
            "(loan_type = 'SHORT_TERM' AND interest_amount IS NOT NULL AND annual_interest_rate IS NULL"
            " AND interest_method IS NULL AND term_months IS NULL)"
            " OR (loan_type = 'LONG_TERM' AND interest_amount IS NULL AND annual_interest_rate IS NOT NULL"
            " AND interest_method IS NOT NULL AND term_months IS NOT NULL)",
            name="shape",
        ),
        sa.CheckConstraint("interest_method IN ('FLAT','ANNUITY')", name="method"),
        sa.CheckConstraint("status IN ('ACTIVE','PAID_OFF','CANCELLED')", name="status"),
        sa.CheckConstraint("principal_amount > 0", name="principal"),
        sa.CheckConstraint("interest_amount >= 0", name="interest"),
        sa.CheckConstraint("annual_interest_rate >= 0 AND annual_interest_rate <= 100", name="rate"),
        sa.CheckConstraint("term_months BETWEEN 1 AND 600", name="term"),
        sa.CheckConstraint("first_due_date >= start_date", name="dates"),
        sa.CheckConstraint(
            "(status = 'CANCELLED') = (cancelled_at IS NOT NULL)"
            " AND (cancelled_at IS NULL) = (cancelled_by IS NULL)"
            " AND (cancelled_at IS NULL) = (cancel_reason IS NULL)",
            name="cancel",
        ),
    )
    op.create_index("ix_loans_lender_id", "loans", ["lender_id"])
    op.create_index("ix_loans_loan_type", "loans", ["loan_type"])
    op.create_index("ix_loans_start_date", "loans", ["start_date"])
    op.create_index("ix_loans_status", "loans", ["status"])

    op.create_table(
        "loan_installments",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("loan_id", sa.BigInteger(), sa.ForeignKey("loans.id", ondelete="CASCADE"), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("due_date", sa.Date(), nullable=False),
        sa.Column("principal_amount", MONEY, nullable=False),
        sa.Column("interest_amount", MONEY, nullable=False),
        sa.Column("principal_paid", MONEY, nullable=False),
        sa.Column("interest_paid", MONEY, nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("paid_date", sa.Date(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("loan_id", "sequence", name="uq_loan_installments_loan_id"),
        sa.CheckConstraint("status IN ('UNPAID','PARTIALLY_PAID','PAID')", name="status"),
        sa.CheckConstraint(
            "principal_amount >= 0 AND interest_amount >= 0 AND principal_paid >= 0 AND interest_paid >= 0"
            " AND principal_paid <= principal_amount AND interest_paid <= interest_amount",
            name="amounts",
        ),
    )
    op.create_index("ix_loan_installments_loan_id", "loan_installments", ["loan_id"])
    op.create_index("ix_loan_installments_due_date", "loan_installments", ["due_date"])
    op.create_index("ix_loan_installments_status", "loan_installments", ["status"])

    op.create_table(
        "loan_repayments",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("loan_id", sa.BigInteger(), sa.ForeignKey("loans.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("paid_date", sa.Date(), nullable=False),
        sa.Column("amount", MONEY, nullable=False),
        sa.Column("principal_amount", MONEY, nullable=False),
        sa.Column("interest_amount", MONEY, nullable=False),
        sa.Column("first_installment", sa.Integer(), nullable=True),
        sa.Column("last_installment", sa.Integer(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_by", sa.BigInteger(), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("voided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("voided_by", sa.BigInteger(), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=True),
        sa.Column("void_reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "amount > 0 AND principal_amount >= 0 AND interest_amount >= 0"
            " AND principal_amount + interest_amount = amount",
            name="amounts",
        ),
        sa.CheckConstraint(
            "(voided_at IS NULL AND voided_by IS NULL AND void_reason IS NULL)"
            " OR (voided_at IS NOT NULL AND voided_by IS NOT NULL AND void_reason IS NOT NULL)",
            name="void",
        ),
    )
    op.create_index("ix_loan_repayments_loan_id", "loan_repayments", ["loan_id"])
    op.create_index("ix_loan_repayments_paid_date", "loan_repayments", ["paid_date"])
    op.create_index("ix_loan_repayments_updated", "loan_repayments", ["updated_at", "id"])

    replace_check(
        "main_cash_account_events",
        "ck_main_cash_account_events_source",
        "source_table IN ('operational_cashflows','revenues','loan_repayments')",
    )
    recreate_log_source_columns(NEW_PATTERN)
    replace_check(
        "cash_statements",
        "ck_cash_statements_source",
        "source_table IN ('operational_cashflows','revenues','main_cash_account_logs','loans')",
    )


def downgrade() -> None:
    op.execute("DELETE FROM cash_statements WHERE source_table = 'loans'")
    replace_check(
        "cash_statements",
        "ck_cash_statements_source",
        "source_table IN ('operational_cashflows','revenues','main_cash_account_logs')",
    )
    # Queued loan movements cannot be kept; posted log entries stay (as plain references).
    op.execute("DELETE FROM main_cash_account_events WHERE source_table = 'loan_repayments'")
    replace_check(
        "main_cash_account_events",
        "ck_main_cash_account_events_source",
        "source_table IN ('operational_cashflows','revenues')",
    )
    recreate_log_source_columns(OLD_PATTERN)
    op.drop_table("loan_repayments")
    op.drop_table("loan_installments")
    op.drop_table("loans")
    # IF EXISTS: also lets this downgrade reset a database built from an earlier draft of this
    # (unreleased) revision, which had no lenders table.
    op.execute("DROP TABLE IF EXISTS lenders")
