import os
import time
from collections.abc import Iterator
from datetime import timedelta
from typing import TYPE_CHECKING
from urllib.parse import urlparse

import pytest

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from app.db.models import User

os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://postgres:postgres@127.0.0.1:5432/pondok_berkah_test")
os.environ.setdefault("API_KEY", "test-api-key-with-at-least-24-characters")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-with-at-least-thirty-two-characters")


# --- Timezone: the tests run in APP_TIMEZONE ----------------------------------------------------
# The app derives every business date in APP_TIMEZONE (app.features.common.today_local), whatever
# the timezone of the machine. The tests take their own dates with today_local() too, and on top
# of that this process is switched to APP_TIMEZONE, so any naive local time (date.today(),
# datetime.now(), a library's) agrees with the app. Without it, a machine whose timezone differs
# from APP_TIMEZONE fails date assertions for part of every day (e.g. UTC vs Asia/Jakarta between
# 17:00 and 24:00 UTC, when Jakarta is already on the next date).
# Done at import, before any test module computes its module-level dates.


def use_app_timezone() -> None:
    from datetime import datetime

    from app.core.config import settings

    os.environ["TZ"] = settings.app_timezone
    if hasattr(time, "tzset"):  # POSIX; elsewhere the tests still use today_local() explicitly
        time.tzset()
        now = datetime.now(settings.timezone)
        if abs(now.replace(tzinfo=None) - datetime.now()) > timedelta(minutes=1):
            raise pytest.UsageError(f"could not switch the test process to APP_TIMEZONE={settings.app_timezone}")


use_app_timezone()


def pytest_sessionstart(session: pytest.Session) -> None:
    database_url = os.environ["DATABASE_URL"]
    database_name = urlparse(database_url.replace("postgresql+psycopg", "postgresql", 1)).path.rsplit("/", 1)[-1]
    if not database_name.endswith("_test"):
        raise pytest.UsageError("DATABASE_URL must target a database whose name ends in _test")


# --- Shared database fixtures for the cashflow / revenue / balance tests ----------------------
# App modules are imported lazily so the environment defaults above are applied first.

TEST_USER_EMAIL = "cash-balance-tests@example.com"
TEST_PASSWORD = "StrongPassword123"


def wipe_cashflow_data(db: Session) -> None:
    from sqlalchemy import delete

    from app.db.models import (
        AuditEvent,
        CashStatement,
        CashStatementSyncRun,
        Customer,
        FinancialOpeningBalance,
        Lender,
        Loan,
        LoanRepayment,
        MainCashAccount,
        MainCashAccountAuditRun,
        MainCashAccountEvent,
        MainCashAccountLog,
        MonthlyCutoff,
        OperationalCashflow,
        OperationalCashflowBalance,
        OperationalCashflowPayment,
        OperationalCashflowTopupDetail,
        OperationalCashflowUtilityDetail,
        Revenue,
        User,
    )

    db.execute(
        delete(AuditEvent).where(
            AuditEvent.entity_type.in_(
                (
                    "operational_cashflows",
                    "revenues",
                    "main_cash_account_logs",
                    "financial_opening_balances",
                    "customers",
                    "loans",
                    "lenders",
                )
            )
        )
    )
    db.execute(delete(MonthlyCutoff))  # details cascade
    db.execute(delete(CashStatement))
    db.execute(delete(CashStatementSyncRun))
    db.execute(delete(LoanRepayment))
    db.execute(delete(Loan))  # installments cascade
    db.execute(delete(Lender))
    db.execute(delete(FinancialOpeningBalance))
    db.execute(delete(OperationalCashflowPayment))
    db.execute(delete(OperationalCashflowTopupDetail))
    db.execute(delete(OperationalCashflowUtilityDetail))
    db.execute(delete(OperationalCashflow))
    db.execute(delete(OperationalCashflowBalance))
    db.execute(delete(Revenue))  # revenue details and payments cascade
    db.execute(delete(Customer))
    db.execute(delete(MainCashAccountAuditRun))
    db.execute(delete(MainCashAccountEvent))
    db.execute(delete(MainCashAccountLog))
    db.execute(delete(MainCashAccount))
    db.execute(delete(AuditEvent).where(AuditEvent.entity_type == "users"))
    db.execute(delete(User).where(User.email == TEST_USER_EMAIL))
    db.commit()


@pytest.fixture
def db() -> Iterator[Session]:
    from app.db.session import SessionLocal

    session = SessionLocal()
    wipe_cashflow_data(session)
    try:
        yield session
    finally:
        session.rollback()
        wipe_cashflow_data(session)
        session.close()


@pytest.fixture
def actor_password() -> str:
    return TEST_PASSWORD


@pytest.fixture
def actor(db: Session) -> User:
    from app.core.security import hash_password
    from app.db.models import User

    user = User(
        name="Finance Admin",
        email=TEST_USER_EMAIL,
        password_hash=hash_password(TEST_PASSWORD),
        role="SUPERADMIN",
        is_active=True,
        must_change_password=False,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user
