"""Create cash_statements, the bank-statement style record of every real cash movement.

No rows are inserted here: the table is populated from the existing transactions by the manual
backfill command (``python -m app.scripts.backfill_cash_statements``); from then on every
cashflow/revenue/manual adjustment write keeps its own rows up to date.

Revision ID: 20261125_0001
Revises: 20261124_0001
Create Date: 2026-11-25
"""

import sqlalchemy as sa

from alembic import op

revision = "20261125_0001"
down_revision = "20261124_0001"
branch_labels = None
depends_on = None

MONEY = sa.Numeric(precision=18, scale=0)


def upgrade() -> None:
    op.create_table(
        "cash_statements",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("statement_date", sa.Date(), nullable=False),
        sa.Column("direction", sa.String(length=10), nullable=False),
        sa.Column("amount", MONEY, nullable=False),
        sa.Column("category", sa.String(length=40), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("counterparty", sa.String(length=255), nullable=True),
        sa.Column("reference", sa.String(length=80), nullable=False),
        sa.Column("source_table", sa.String(length=40), nullable=False),
        sa.Column("source_id", sa.BigInteger(), nullable=False),
        sa.Column("source_line", sa.String(length=60), nullable=False),
        sa.Column("payment_id", sa.BigInteger(), nullable=True),
        sa.Column("payment_sequence", sa.Integer(), nullable=True),
        sa.Column("source_transaction_date", sa.Date(), nullable=False),
        sa.Column("source_amount", MONEY, nullable=False),
        sa.Column("source_payment_status", sa.String(length=20), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("direction IN ('credit','debit')", name="direction"),
        sa.CheckConstraint("amount > 0", name="amount"),
        sa.CheckConstraint("source_table IN ('operational_cashflows','revenues','main_cash_account_logs')", name="source"),
        sa.CheckConstraint("(payment_id IS NULL) = (payment_sequence IS NULL)", name="payment"),
        sa.UniqueConstraint("source_table", "source_id", "source_line", name="uq_cash_statements_source_table"),
    )
    op.create_index("ix_cash_statements_date_id", "cash_statements", ["statement_date", "id"])
    op.create_index("ix_cash_statements_direction", "cash_statements", ["direction"])
    op.create_index("ix_cash_statements_category", "cash_statements", ["category"])
    op.create_index("ix_cash_statements_reference", "cash_statements", ["reference"])


def downgrade() -> None:
    op.drop_table("cash_statements")
