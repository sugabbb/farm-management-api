"""Create main_cash_accounts and main_cash_account_logs.

No row is inserted here: the single account row is created on first use, and existing
top-ups/transfers are posted to it by the manual backfill command
(``python -m app.scripts.backfill_main_cash_account``).

Revision ID: 20261119_0001
Revises: 20261118_0001
Create Date: 2026-11-19
"""

import sqlalchemy as sa

from alembic import op

revision = "20261119_0001"
down_revision = "20261118_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "main_cash_accounts",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("current_amount", sa.Numeric(precision=18, scale=0), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    # At most one main cash account can ever exist.
    op.create_index("uq_main_cash_accounts_singleton", "main_cash_accounts", [sa.text("(true)")], unique=True)

    op.create_table(
        "main_cash_account_logs",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "main_cash_account_id",
            sa.BigInteger(),
            sa.ForeignKey("main_cash_accounts.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("amount", sa.Numeric(precision=18, scale=0), nullable=False),
        sa.Column("direction", sa.String(length=10), nullable=False),
        sa.Column("reference", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("direction IN ('credit','debit')", name="direction"),
        sa.CheckConstraint("amount > 0", name="amount"),
    )
    op.create_index(
        "ix_main_cash_account_logs_main_cash_account_id", "main_cash_account_logs", ["main_cash_account_id"]
    )
    op.create_index("ix_main_cash_account_logs_direction", "main_cash_account_logs", ["direction"])
    op.create_index("ix_main_cash_account_logs_created_at", "main_cash_account_logs", ["created_at"])


def downgrade() -> None:
    op.drop_table("main_cash_account_logs")
    op.drop_table("main_cash_accounts")
