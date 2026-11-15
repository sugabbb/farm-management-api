"""Create the operational_cashflow_balances table.

Revision ID: 20261116_0001
Revises: 20261115_0001
Create Date: 2026-11-16
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "20261116_0001"
down_revision = "20261115_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "operational_cashflow_balances",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("type", sa.String(length=30), nullable=False),
        sa.Column("amount", sa.Numeric(precision=18, scale=0), nullable=False, server_default="0"),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint("type IN ('cash_on_hand')", name="type"),
    )
    op.create_index(
        "ix_operational_cashflow_balances_type",
        "operational_cashflow_balances",
        ["type"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ix_operational_cashflow_balances_type", table_name="operational_cashflow_balances")
    op.drop_table("operational_cashflow_balances")
