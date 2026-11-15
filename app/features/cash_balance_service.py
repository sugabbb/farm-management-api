"""Maintains the running operational cashflow balances (e.g. cash on hand).

The module keeps a single row per ``operational_cashflow_balances.type``. Every time a
transaction happens on ``operational_cashflows`` (creation, correction, payment, or payment
void) the affected balance is adjusted in place rather than recomputed from the full history,
so the table always has exactly one up to date row per type.

Rules implemented for ``cash_on_hand``:

- ``MAIN_ACCOUNT_TOPUP`` transactions add ``amount_idr`` to the balance directly.
- Any other cashflow type reduces the balance by ``amount_idr`` once its ``payment_status``
  becomes ``PAID`` or ``PARTIALLY_PAID``.
"""

from decimal import Decimal
from typing import Any

from sqlalchemy import ColumnElement, case, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.db.models import OperationalCashflow, OperationalCashflowBalance

CASH_ON_HAND = "cash_on_hand"

PAID_STATUSES = ("PAID", "PARTIALLY_PAID")


def get_balance(db: Session, balance_type: str, *, lock: bool = False) -> OperationalCashflowBalance | None:
    stmt = select(OperationalCashflowBalance).where(OperationalCashflowBalance.type == balance_type)
    if lock:
        stmt = stmt.with_for_update()
    return db.scalar(stmt)


def get_or_create_balance(db: Session, balance_type: str) -> OperationalCashflowBalance:
    """Returns the single balance row for ``balance_type``, creating it with amount 0 if the
    table has no row for that type yet. Locks the row while mutating so concurrent
    transactions cannot corrupt the running total.
    """
    balance = get_balance(db, balance_type, lock=True)
    if balance is None:
        # ON CONFLICT keeps "one row per type" safe even if two sessions seed at the same time.
        db.execute(
            insert(OperationalCashflowBalance)
            .values(type=balance_type, amount=Decimal("0"))
            .on_conflict_do_nothing(index_elements=["type"])
        )
        balance = get_balance(db, balance_type, lock=True)
        assert balance is not None
    return balance


def cashflow_effect(cashflow_type: str, payment_status: str, amount_idr: Decimal) -> Decimal:
    """Returns the signed contribution of an operational cashflow transaction, in its current
    persisted state, to the ``cash_on_hand`` balance.
    """
    if cashflow_type == "MAIN_ACCOUNT_TOPUP":
        return amount_idr if payment_status == "PAID" else Decimal("0")
    return -amount_idr if payment_status in PAID_STATUSES else Decimal("0")


def cashflow_effect_sql() -> ColumnElement[Decimal]:
    """SQL twin of :func:`cashflow_effect`, used to recalculate the balance from the whole
    ``operational_cashflows`` table in a single aggregate query. Keep both in sync.
    """
    return case(
        (
            (OperationalCashflow.cashflow_type == "MAIN_ACCOUNT_TOPUP")
            & (OperationalCashflow.payment_status == "PAID"),
            OperationalCashflow.amount_idr,
        ),
        (
            (OperationalCashflow.cashflow_type != "MAIN_ACCOUNT_TOPUP")
            & OperationalCashflow.payment_status.in_(PAID_STATUSES),
            -OperationalCashflow.amount_idr,
        ),
        else_=Decimal("0"),
    )


def calculate_cash_on_hand(db: Session) -> tuple[Decimal, int]:
    """Recalculates cash on hand from scratch over every operational cashflow.

    Returns ``(amount, counted_transactions)`` where ``counted_transactions`` is the number of
    rows that actually moved the balance (paid top-ups and paid/partially-paid outflows).
    """
    effect = cashflow_effect_sql()
    total, counted = db.execute(
        select(
            func.coalesce(func.sum(effect), Decimal("0")),
            func.count().filter(effect != 0),
        ).select_from(OperationalCashflow)
    ).one()
    return Decimal(total), int(counted)


def snapshot_effect(snapshot: dict[str, Any] | None) -> Decimal:
    if snapshot is None:
        return Decimal("0")
    return cashflow_effect(snapshot["cashflow_type"], snapshot["payment_status"], Decimal(str(snapshot["amount_idr"])))


def sync_cash_on_hand(db: Session, before: dict[str, Any] | None, row: OperationalCashflow) -> None:
    """Applies the delta between the transaction's previous and current state to the
    ``cash_on_hand`` balance. Safe to call after every create/update/payment/void on an
    operational cashflow row; it only ever applies the net change, so it never double counts.
    """
    before_effect = snapshot_effect(before)
    after_effect = cashflow_effect(row.cashflow_type, row.payment_status, Decimal(row.amount_idr))
    delta = after_effect - before_effect
    if delta == 0:
        return
    balance = get_or_create_balance(db, CASH_ON_HAND)
    balance.amount = Decimal(balance.amount) + delta
    db.flush()


def cash_on_hand(db: Session) -> OperationalCashflowBalance:
    """Read-only accessor used by the API. Seeds the row with amount 0 the first time it is
    requested if no transaction has created it yet.
    """
    balance = get_balance(db, CASH_ON_HAND)
    if balance is None:
        balance = get_or_create_balance(db, CASH_ON_HAND)
        db.commit()
    return balance
