"""Populate ``operational_cashflow_balances`` (type ``cash_on_hand``) from ``operational_cashflows``.

This is a manual data migration, independent of the Alembic schema migrations. It recalculates
cash on hand from every existing operational cashflow and overwrites the stored balance, so it is
idempotent and safe to run at any time (including repeatedly, or later to repair drift):

- ``MAIN_ACCOUNT_TOPUP`` with payment_status ``PAID`` adds ``amount_idr``.
- Any other cashflow type with payment_status ``PAID`` / ``PARTIALLY_PAID`` subtracts ``amount_idr``.

It takes the same transactional advisory lock that every operational cashflow write uses, so it
can run while the API is live: concurrent cashflow writes simply wait until it commits.

Usage (from the backend directory, with DATABASE_URL set or present in .env)::

    python -m app.scripts.backfill_cash_on_hand --dry-run   # show what would be written
    python -m app.scripts.backfill_cash_on_hand             # write the balance
"""

import argparse
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy.orm import Session

from app.core.decimal import decimal_string
from app.db.session import SessionLocal
from app.features.cash_balance_service import (
    CASH_ON_HAND,
    calculate_cash_on_hand,
    get_balance,
    get_or_create_balance,
)
from app.features.farm_service import lock_balances


@dataclass(frozen=True)
class BackfillResult:
    previous_amount: Decimal | None
    new_amount: Decimal
    counted_transactions: int
    dry_run: bool

    @property
    def changed(self) -> bool:
        return self.previous_amount != self.new_amount


def backfill_cash_on_hand(db: Session, *, dry_run: bool = False) -> BackfillResult:
    """Recalculates cash on hand and stores it. Commits unless ``dry_run`` (then rolls back)."""
    try:
        lock_balances(db)
        existing = get_balance(db, CASH_ON_HAND, lock=True)
        previous = Decimal(existing.amount) if existing else None
        amount, counted = calculate_cash_on_hand(db)
        if not dry_run:
            balance = get_or_create_balance(db, CASH_ON_HAND)
            balance.amount = amount
        result = BackfillResult(previous, amount, counted, dry_run)
        if dry_run:
            db.rollback()
        else:
            db.commit()
        return result
    except Exception:
        db.rollback()
        raise


def format_amount(value: Decimal | None) -> str:
    return "(no row yet)" if value is None else (decimal_string(value, 0) or "0")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.scripts.backfill_cash_on_hand",
        description="Recalculate the cash_on_hand balance from operational_cashflows and save it.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="calculate and print the balance without writing anything",
    )
    args = parser.parse_args(argv)

    with SessionLocal() as db:
        result = backfill_cash_on_hand(db, dry_run=args.dry_run)

    print(f"Transactions affecting cash on hand: {result.counted_transactions}")
    print(f"Previous cash_on_hand amount:        {format_amount(result.previous_amount)}")
    print(f"Recalculated cash_on_hand amount:    {format_amount(result.new_amount)}")
    if result.dry_run:
        print("Dry run: nothing was written.")
    elif result.changed:
        print("cash_on_hand balance updated.")
    else:
        print("cash_on_hand balance already up to date; nothing to change.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
