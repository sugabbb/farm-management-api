from app.db import models  # noqa: F401
from app.db.base import Base


def test_schema_contains_only_retained_tables() -> None:
    expected = {
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
        "operational_cashflow_balances",
        "revenues",
        "revenue_egg_details",
        "revenue_fish_details",
        "revenue_other_details",
        "revenue_transfer_details",
        "revenue_payments",
        "lele_samplings",
        "lele_sampling_measurements",
        "audit_events",
    }
    assert set(Base.metadata.tables) == expected
    assert not {"sessions", "abk_submissions", "app_settings", "lele_growth_profiles", "lele_growth_points"} & set(
        Base.metadata.tables
    )
