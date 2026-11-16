"""Allow the approved_unpaid type in operational_cashflow_balances.

The balance row itself is not created here. It is seeded from existing approved cashflows (and
their non-voided payments) by the manual backfill command
(``python -m app.scripts.backfill_cash_on_hand``), then kept up to date by every operational
cashflow write.

Revision ID: 20261118_0001
Revises: 20261117_0001
Create Date: 2026-11-18
"""

from alembic import op

revision = "20261118_0001"
down_revision = "20261117_0001"
branch_labels = None
depends_on = None

TABLE = "operational_cashflow_balances"
# op.f(): the name is already final, do not run it through the metadata naming convention again.
CONSTRAINT = "ck_operational_cashflow_balances_type"


def upgrade() -> None:
    op.drop_constraint(op.f(CONSTRAINT), TABLE, type_="check")
    op.create_check_constraint(op.f(CONSTRAINT), TABLE, "type IN ('cash_on_hand','waiting_approval','approved_unpaid')")


def downgrade() -> None:
    op.execute(f"DELETE FROM {TABLE} WHERE type = 'approved_unpaid'")
    op.drop_constraint(op.f(CONSTRAINT), TABLE, type_="check")
    op.create_check_constraint(op.f(CONSTRAINT), TABLE, "type IN ('cash_on_hand','waiting_approval')")
