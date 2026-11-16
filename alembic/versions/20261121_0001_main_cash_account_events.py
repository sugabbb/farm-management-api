"""Post main cash account movements asynchronously through an outbox table.

Cashflow/revenue transactions now only insert into ``main_cash_account_events``; a background
worker posts them to ``main_cash_account_logs``. Also records how many pending events each audit
run posted.

Revision ID: 20261121_0001
Revises: 20261120_0001
Create Date: 2026-11-21
"""

import sqlalchemy as sa

from alembic import op

revision = "20261121_0001"
down_revision = "20261120_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "main_cash_account_events",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("source_table", sa.String(length=40), nullable=False),
        sa.Column("source_id", sa.BigInteger(), nullable=False),
        sa.Column("action", sa.String(length=100), nullable=False),
        sa.Column("delta", sa.Numeric(precision=18, scale=0), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("source_table IN ('operational_cashflows','revenues')", name="source"),
        sa.CheckConstraint("delta <> 0", name="delta"),
    )
    op.create_index("ix_main_cash_account_events_source", "main_cash_account_events", ["source_table", "source_id"])
    op.add_column(
        "main_cash_account_audit_runs",
        sa.Column("events_processed", sa.BigInteger(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    # Post anything still queued first would need the app; refuse to silently drop money movements.
    pending = op.get_bind().execute(sa.text("SELECT count(*) FROM main_cash_account_events")).scalar()
    if pending:
        raise RuntimeError(
            f"{pending} main cash account events are still pending; run "
            "`python -m app.scripts.main_cash_account_worker --once` before downgrading."
        )
    op.drop_column("main_cash_account_audit_runs", "events_processed")
    op.drop_table("main_cash_account_events")
