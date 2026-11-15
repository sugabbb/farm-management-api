"""Create the empty transactional schema.

Revision ID: 20261115_0001
Revises: None
Create Date: 2026-11-15
"""

from alembic import op
from app.db import models  # noqa: F401
from app.db.base import Base

revision = "20261115_0001"
down_revision = None
branch_labels = None
depends_on = None

# Pinned to the tables that existed when this migration was written. Tables added by later
# models (e.g. operational_cashflow_balances) get their own migration and must not be created
# here, otherwise replaying the full migration history against an empty database would try to
# create them twice.
TABLE_NAMES = [
    "users",
    "auth_login_attempts",
    "employees",
    "livestock_groups",
    "feed_types",
    "daily_records",
    "feed_usages",
    "egg_harvests",
    "inventory_opening_balances",
    "inventory_adjustments",
    "population_adjustments",
    "financial_opening_balances",
    "customers",
    "operational_cashflows",
    "operational_cashflow_feed_details",
    "operational_cashflow_salary_details",
    "operational_cashflow_utility_details",
    "operational_cashflow_health_details",
    "operational_cashflow_other_details",
    "operational_cashflow_topup_details",
    "operational_cashflow_livestock_purchase_details",
    "operational_cashflow_bank_admin_details",
    "operational_cashflow_payments",
    "revenues",
    "revenue_egg_details",
    "revenue_fish_details",
    "revenue_other_details",
    "revenue_transfer_details",
    "revenue_payments",
    "lele_samplings",
    "lele_sampling_measurements",
    "audit_events",
]


def tables() -> list:
    return [Base.metadata.tables[name] for name in TABLE_NAMES]


def upgrade() -> None:
    Base.metadata.create_all(bind=op.get_bind(), tables=tables(), checkfirst=False)


def downgrade() -> None:
    Base.metadata.drop_all(bind=op.get_bind(), tables=tables(), checkfirst=False)
