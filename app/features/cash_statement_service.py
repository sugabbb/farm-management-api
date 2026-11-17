"""Populates ``cash_statements``: every real cash movement of the business, like a bank statement.

A statement row is written only when money actually moves, dated on the day it moved, with
``credit`` = money in and ``debit`` = money out. Three sources feed it:

``operational_cashflows`` (every type except ``MAIN_ACCOUNT_TOPUP``) -> **debit**
    Once the cashflow is ``PARTIALLY_PAID`` or ``PAID``: one row per non-voided payment, for the
    amount of that payment, on its ``paid_date`` (``source_line`` = ``payment:<payment id>``). A
    cashflow paid in three parts therefore has three rows, numbered in their description
    ("partial payment 1", "partial payment 2", "final payment 3") and sharing the same
    ``reference``. A ``PAID`` cashflow without any payment row (data recorded outside the API) is
    one row for its full amount on its paid date (``paid``).

``revenues`` (every type except ``MAIN_ACCOUNT_TRANSFER``) -> **credit**
    Same rules: one row per non-voided payment of a ``PARTIALLY_PAID``/``PAID`` revenue, or one
    row for the full amount of a ``PAID`` revenue without payment rows - in practice an approved
    ``DIRECT_TRANSFER`` sale, dated on its transfer date (``paid``).

``revenues`` of type ``MAIN_ACCOUNT_TRANSFER`` -> **debit of the fee only**
    The transfer itself only moves money between the business's own accounts, so it is not a
    statement row; its ``payment_cost_idr`` is money that leaves the business, recorded once the
    transfer is ``PAID`` (approved) on its paid date (``fee``).

``main_cash_account_logs`` -> the manual adjustments only
    Entries referenced ``manual:<note>``, in their own direction, on the date they were posted
    (``manual``). The entries posted automatically for top-ups, transfers and loan repayments are
    not included (loan repayments have their own rows, below).

``loans`` (their non-voided ``loan_repayments``) -> **debit**
    One row per repayment, for its full amount (principal + interest), on its ``paid_date``
    (``LOAN_REPAYMENT``, ``repayment:<id>``), like the money that left the bank. The
    principal/interest split stays in the loan tables. All rows of a loan share the reference
    ``loans:<loan id>``; ``payment_id`` is the repayment and ``payment_sequence`` its number. The
    source details are the loan's start date, principal and ``PARTIALLY_PAID``/``PAID`` (paid off).

The top-ups (``MAIN_ACCOUNT_TOPUP``) are excluded for the same reason as the transfers: they move
money from the main account to the operational cash, both of which belong to the business.

Every row also records where it comes from: ``reference`` (``<source table>:<source id>``, the
same for all rows of a transaction), ``source_table``/``source_id``/``source_line``,
``payment_id``/``payment_sequence`` for payment rows, and the current ``source_transaction_date``,
``source_amount`` (for a transfer fee, the transfer's gross amount) and
``source_payment_status`` of the transaction.

Each rule is a SQL ``SELECT`` producing the statement rows of its source, so the table is written
set-based: :func:`sync_cash_statements` replaces the rows of one source transaction inside the
caller's transaction (called after every cashflow/revenue/adjustment write), and
:func:`rebuild_cash_statements` does the same for the whole table (the backfill command). Both are
idempotent: a row whose values did not change is left untouched (its ``id`` is stable), rows whose
source no longer qualifies (voided payment, corrected amount, ...) are updated or deleted.
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    ColumnElement,
    CompoundSelect,
    Date,
    Integer,
    Numeric,
    Select,
    SQLColumnExpression,
    String,
    Text,
    and_,
    case,
    cast,
    delete,
    exists,
    func,
    literal,
    or_,
    select,
    union_all,
)
from sqlalchemy.dialects.postgresql import aggregate_order_by, insert
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models import (
    CashStatement,
    Customer,
    Employee,
    FeedType,
    Lender,
    Loan,
    LoanRepayment,
    MainCashAccountLog,
    OperationalCashflow,
    OperationalCashflowBankAdminDetail,
    OperationalCashflowFeedDetail,
    OperationalCashflowHealthDetail,
    OperationalCashflowLivestockPurchaseDetail,
    OperationalCashflowOtherDetail,
    OperationalCashflowPayment,
    OperationalCashflowSalaryDetail,
    OperationalCashflowUtilityDetail,
    Revenue,
    RevenueFishDetail,
    RevenueOtherDetail,
    RevenuePayment,
    RevenueTransferDetail,
)
from app.features.farm_service import lock_balances
from app.features.main_cash_account_service import CREDIT, DEBIT, MANUAL_SOURCE, TOPUP_TYPE, TRANSFER_TYPE

CASHFLOW_SOURCE = "operational_cashflows"
REVENUE_SOURCE = "revenues"
LOG_SOURCE = "main_cash_account_logs"
LOAN_SOURCE = "loans"
SOURCES = (CASHFLOW_SOURCE, REVENUE_SOURCE, LOG_SOURCE, LOAN_SOURCE)

PAID_STATUSES = ("PARTIALLY_PAID", "PAID")
FEE_CATEGORY = "TRANSFER_FEE"
MANUAL_CATEGORY = "MANUAL_ADJUSTMENT"
LOAN_REPAYMENT_CATEGORY = "LOAN_REPAYMENT"

# Columns written by the sync, in SELECT order.
COLUMNS = (
    "statement_date",
    "direction",
    "amount",
    "category",
    "description",
    "counterparty",
    "reference",
    "source_table",
    "source_id",
    "source_line",
    "payment_id",
    "payment_sequence",
    "source_transaction_date",
    "source_amount",
    "source_payment_status",
)
KEY_COLUMNS = ("source_table", "source_id", "source_line")
# Everything except the key: a change in any of them makes a stored row stale.
VALUE_COLUMNS = tuple(name for name in COLUMNS if name not in KEY_COLUMNS)
COLUMN_TYPES: dict[str, Any] = {
    "statement_date": Date(),
    "direction": String(10),
    "amount": Numeric(18, 0),
    "category": String(40),
    "description": Text(),
    "counterparty": String(255),
    "reference": String(80),
    "source_table": String(40),
    "source_id": BigInteger(),
    "source_line": String(60),
    "payment_id": BigInteger(),
    "payment_sequence": Integer(),
    "source_transaction_date": Date(),
    "source_amount": Numeric(18, 0),
    "source_payment_status": String(20),
}

Expression = SQLColumnExpression[Any]


# --- Building blocks ----------------------------------------------------------------------------


def local_date(timestamp: Expression) -> ColumnElement[date]:
    """Business date of a timestamp, in the application timezone."""
    return cast(func.timezone(settings.app_timezone, timestamp), Date)


def paid_date(paid_at: Expression, transaction_date: Expression) -> ColumnElement[date]:
    """Date a transaction without payment rows was paid (its transaction date if never stamped)."""
    return func.coalesce(local_date(paid_at), transaction_date)


def with_notes(base: str | Expression, notes: Expression) -> ColumnElement[str]:
    """``base`` followed by ``: notes`` when the notes are not empty."""
    prefix = literal(base) if isinstance(base, str) else base
    return prefix + func.coalesce(literal(": ") + func.nullif(notes, ""), "")


def reference(source_table: str, source_id: Expression) -> ColumnElement[str]:
    return literal(f"{source_table}:") + cast(source_id, String)


def statement_row(**values: Any) -> list[ColumnElement[Any]]:
    """The SELECT list of one rule: every column of ``COLUMNS`` (missing ones are NULL), cast to
    the table's type and labelled, so all rules can be combined with UNION ALL."""
    unknown = set(values) - set(COLUMNS)
    if unknown:
        raise ValueError(f"unknown cash statement columns {sorted(unknown)}")
    columns: list[ColumnElement[Any]] = []
    for name in COLUMNS:
        value = values.get(name)
        expression = literal(value) if value is None or isinstance(value, str | int) else value
        columns.append(cast(expression, COLUMN_TYPES[name]).label(name))
    return columns


def payment_suffix(
    sequence: Expression, count: Expression, cumulative: Expression, total: Expression
) -> ColumnElement[str]:
    """Tells the payments of one transaction apart in their description: nothing when a single
    payment settled it, otherwise "partial payment <n>" and, for the one that settled it,
    "final payment <n>"."""
    number = cast(sequence, String) + literal(")")
    return case(
        (and_(count == 1, cumulative >= total), literal("")),
        (cumulative >= total, literal(" (final payment ") + number),
        else_=literal(" (partial payment ") + number,
    )


@dataclass(frozen=True)
class PaymentWindow:
    """Sequence, count and running total of the non-voided payments of each transaction."""

    sequence: ColumnElement[Any]
    count: ColumnElement[Any]
    cumulative: ColumnElement[Any]

    @classmethod
    def over(
        cls, transaction_id: Expression, paid_on: Expression, payment_id: Expression, amount: Expression
    ) -> PaymentWindow:
        order = (paid_on, payment_id)
        return cls(
            func.row_number().over(partition_by=transaction_id, order_by=order),
            func.count().over(partition_by=transaction_id),
            func.sum(amount).over(partition_by=transaction_id, order_by=order),
        )


# --- Rules: operational cashflows ---------------------------------------------------------------


def cashflow_base() -> tuple[Select[Any], Expression, Expression]:
    """``SELECT FROM operational_cashflows`` joined to its detail tables, with the description and
    counterparty of a cashflow. Shared by the cashflow rules."""
    cashflow = OperationalCashflow
    salary, utility, health, other = (
        OperationalCashflowSalaryDetail,
        OperationalCashflowUtilityDetail,
        OperationalCashflowHealthDetail,
        OperationalCashflowOtherDetail,
    )
    livestock, bank = OperationalCashflowLivestockPurchaseDetail, OperationalCashflowBankAdminDetail
    feed_names = (
        select(func.string_agg(FeedType.name, aggregate_order_by(literal(", "), FeedType.name)))
        .select_from(OperationalCashflowFeedDetail)
        .join(FeedType, FeedType.id == OperationalCashflowFeedDetail.feed_type_id)
        .where(OperationalCashflowFeedDetail.operational_cashflow_id == cashflow.id)
        .correlate(cashflow)
        .scalar_subquery()
    )
    description = case(
        (cashflow.cashflow_type == "FEED_AYAM", with_notes("Feed purchase Ayam", feed_names)),
        (cashflow.cashflow_type == "FEED_LELE", with_notes("Feed purchase Lele", feed_names)),
        (cashflow.cashflow_type == "SALARY", with_notes("Salary", salary.salary_period)),
        (cashflow.cashflow_type == "UTILITY", with_notes("Utility", utility.description)),
        (
            cashflow.cashflow_type == "HEALTH",
            with_notes(literal("Health ") + func.initcap(health.species), health.description),
        ),
        (cashflow.cashflow_type == "OTHER", with_notes("Other expense", other.description)),
        (cashflow.cashflow_type == "LELE_SEED_PURCHASE", with_notes("Lele seed purchase", livestock.notes)),
        (cashflow.cashflow_type == "PULLET_PURCHASE", with_notes("Pullet purchase", livestock.notes)),
        (cashflow.cashflow_type == "BANK_ADMIN_FEE", with_notes("Bank admin fee", bank.notes)),
        else_=cashflow.cashflow_type,
    )
    stmt = (
        select(cashflow)
        .outerjoin(salary, salary.operational_cashflow_id == cashflow.id)
        .outerjoin(Employee, Employee.id == salary.employee_id)
        .outerjoin(utility, utility.operational_cashflow_id == cashflow.id)
        .outerjoin(health, health.operational_cashflow_id == cashflow.id)
        .outerjoin(other, other.operational_cashflow_id == cashflow.id)
        .outerjoin(livestock, livestock.operational_cashflow_id == cashflow.id)
        .outerjoin(bank, bank.operational_cashflow_id == cashflow.id)
        .where(cashflow.cashflow_type != TOPUP_TYPE)
    )
    return stmt, description, Employee.name


def cashflow_rules(source_id: int | None) -> list[Select[Any]]:
    cashflow, payment = OperationalCashflow, OperationalCashflowPayment
    base, description, counterparty = cashflow_base()
    if source_id is not None:
        base = base.where(cashflow.id == source_id)
    source = {
        "category": cashflow.cashflow_type,
        "counterparty": counterparty,
        "reference": reference(CASHFLOW_SOURCE, cashflow.id),
        "source_table": CASHFLOW_SOURCE,
        "source_id": cashflow.id,
        "source_transaction_date": cashflow.transaction_date,
        "source_amount": cashflow.amount_idr,
        "source_payment_status": cashflow.payment_status,
    }
    window = PaymentWindow.over(payment.operational_cashflow_id, payment.paid_date, payment.id, payment.amount_idr)
    payments = (
        base.join(payment, payment.operational_cashflow_id == cashflow.id)
        .with_only_columns(
            *statement_row(
                **source,
                statement_date=payment.paid_date,
                direction=DEBIT,
                amount=payment.amount_idr,
                description=description
                + payment_suffix(window.sequence, window.count, window.cumulative, cashflow.amount_idr),
                source_line=literal("payment:") + cast(payment.id, String),
                payment_id=payment.id,
                payment_sequence=window.sequence,
            )
        )
        .where(cashflow.payment_status.in_(PAID_STATUSES), payment.voided_at.is_(None), payment.amount_idr > 0)
    )
    has_payment = exists().where(payment.operational_cashflow_id == cashflow.id, payment.voided_at.is_(None))
    settled_without_payments = base.with_only_columns(
        *statement_row(
            **source,
            statement_date=paid_date(cashflow.paid_at, cashflow.transaction_date),
            direction=DEBIT,
            amount=cashflow.amount_idr,
            description=description,
            source_line="paid",
        )
    ).where(cashflow.payment_status == "PAID", cashflow.amount_idr > 0, ~has_payment)
    return [payments, settled_without_payments]


# --- Rules: revenues ----------------------------------------------------------------------------


def revenue_rules(source_id: int | None) -> list[Select[Any]]:
    revenue, payment, transfer = Revenue, RevenuePayment, RevenueTransferDetail
    other, fish = RevenueOtherDetail, RevenueFishDetail
    description = case(
        (revenue.revenue_type == "EGG_SALE", literal("Egg sale")),
        (revenue.revenue_type == "FISH_SALE", with_notes("Fish sale", fish.notes)),
        (revenue.revenue_type == "OTHER", with_notes("Other revenue", other.notes)),
        else_=revenue.revenue_type,
    )
    base = (
        select(revenue)
        .outerjoin(Customer, Customer.id == revenue.customer_id)
        .outerjoin(other, other.revenue_id == revenue.id)
        .outerjoin(fish, fish.revenue_id == revenue.id)
        .outerjoin(transfer, transfer.revenue_id == revenue.id)
    )
    if source_id is not None:
        base = base.where(revenue.id == source_id)
    source = {
        "counterparty": Customer.name,
        "reference": reference(REVENUE_SOURCE, revenue.id),
        "source_table": REVENUE_SOURCE,
        "source_id": revenue.id,
        "source_transaction_date": revenue.transaction_date,
        "source_amount": revenue.amount_idr,
        "source_payment_status": revenue.payment_status,
    }
    sale = revenue.revenue_type != TRANSFER_TYPE
    window = PaymentWindow.over(payment.revenue_id, payment.paid_date, payment.id, payment.amount_idr)
    payments = (
        base.join(payment, payment.revenue_id == revenue.id)
        .with_only_columns(
            *statement_row(
                **source,
                statement_date=payment.paid_date,
                direction=CREDIT,
                amount=payment.amount_idr,
                category=revenue.revenue_type,
                description=description
                + payment_suffix(window.sequence, window.count, window.cumulative, revenue.amount_idr),
                source_line=literal("payment:") + cast(payment.id, String),
                payment_id=payment.id,
                payment_sequence=window.sequence,
            )
        )
        .where(sale, revenue.payment_status.in_(PAID_STATUSES), payment.voided_at.is_(None), payment.amount_idr > 0)
    )
    has_payment = exists().where(payment.revenue_id == revenue.id, payment.voided_at.is_(None))
    settled_without_payments = base.with_only_columns(
        *statement_row(
            **source,
            statement_date=paid_date(revenue.paid_at, revenue.transaction_date),
            direction=CREDIT,
            amount=revenue.amount_idr,
            category=revenue.revenue_type,
            description=description
            + case((revenue.payment_method == "DIRECT_TRANSFER", literal(" (direct transfer)")), else_=literal("")),
            source_line="paid",
        )
    ).where(sale, revenue.payment_status == "PAID", revenue.amount_idr > 0, ~has_payment)
    transfer_fee = base.with_only_columns(
        *statement_row(
            **(source | {"counterparty": None, "source_amount": transfer.gross_amount_idr}),
            statement_date=paid_date(revenue.paid_at, revenue.transaction_date),
            direction=DEBIT,
            amount=transfer.payment_cost_idr,
            category=FEE_CATEGORY,
            description=with_notes("Main account transfer fee", transfer.notes),
            source_line="fee",
        )
    ).where(revenue.revenue_type == TRANSFER_TYPE, revenue.payment_status == "PAID", transfer.payment_cost_idr > 0)
    return [payments, settled_without_payments, transfer_fee]


# --- Rules: main cash account adjustments -------------------------------------------------------


def manual_rule(source_id: int | None) -> Select[Any]:
    log = MainCashAccountLog
    prefix = f"{MANUAL_SOURCE}:"
    posted_on = local_date(log.created_at)
    stmt = select(
        *statement_row(
            statement_date=posted_on,
            direction=log.direction,
            amount=log.amount,
            category=MANUAL_CATEGORY,
            description=with_notes("Main account adjustment", func.substr(log.reference, len(prefix) + 1)),
            reference=reference(LOG_SOURCE, log.id),
            source_table=LOG_SOURCE,
            source_id=log.id,
            source_line="manual",
            source_transaction_date=posted_on,
            source_amount=log.amount,
        )
    ).where(func.starts_with(log.reference, prefix))
    return stmt.where(log.id == source_id) if source_id is not None else stmt


# --- Rules: loan repayments ---------------------------------------------------------------------


def loan_rules(source_id: int | None) -> list[Select[Any]]:
    loan, repayment = Loan, LoanRepayment
    lender = Lender.name + func.coalesce(literal(" ") + func.nullif(loan.reference_number, ""), "")
    first, last = cast(repayment.first_installment, String), cast(repayment.last_installment, String)
    # A short-term loan has a single installment: nothing to tell apart.
    installments = func.coalesce(
        case(
            (loan.loan_type == "SHORT_TERM", literal("")),
            (repayment.first_installment == repayment.last_installment, literal(" (installment ") + first + ")"),
            else_=literal(" (installments ") + first + "-" + last + ")",
        ),
        "",
    )
    sequence = func.row_number().over(partition_by=repayment.loan_id, order_by=(repayment.paid_date, repayment.id))
    stmt = (
        select(repayment)
        .join(loan, loan.id == repayment.loan_id)
        .join(Lender, Lender.id == loan.lender_id)
        .with_only_columns(
            *statement_row(
                statement_date=repayment.paid_date,
                direction=DEBIT,
                amount=repayment.amount,
                category=LOAN_REPAYMENT_CATEGORY,
                description=literal("Loan repayment: ") + lender + installments,
                counterparty=Lender.name,
                reference=reference(LOAN_SOURCE, loan.id),
                source_table=LOAN_SOURCE,
                source_id=loan.id,
                source_line=literal("repayment:") + cast(repayment.id, String),
                payment_id=repayment.id,
                payment_sequence=sequence,
                source_transaction_date=loan.start_date,
                source_amount=loan.principal_amount,
                source_payment_status=case(
                    (loan.status == "PAID_OFF", literal("PAID")), else_=literal("PARTIALLY_PAID")
                ),
            )
        )
        .where(repayment.voided_at.is_(None))
    )
    return [stmt.where(repayment.loan_id == source_id) if source_id is not None else stmt]


def expected_statements(source_table: str | None = None, source_id: int | None = None) -> CompoundSelect[Any]:
    """Every statement row the sources currently justify, optionally only those of one source row."""
    rules: list[Select[Any]] = []
    if source_table in (None, CASHFLOW_SOURCE):
        rules.extend(cashflow_rules(source_id))
    if source_table in (None, REVENUE_SOURCE):
        rules.extend(revenue_rules(source_id))
    if source_table in (None, LOG_SOURCE):
        rules.append(manual_rule(source_id))
    if source_table in (None, LOAN_SOURCE):
        rules.extend(loan_rules(source_id))
    return union_all(*rules)


# --- Sync ---------------------------------------------------------------------------------------


@dataclass(frozen=True)
class SyncPlan:
    """What a sync would change: rows to insert, stored rows whose values changed, and stored rows
    whose source no longer justifies them."""

    inserted: int
    updated: int
    deleted: int

    @property
    def changed(self) -> bool:
        return bool(self.inserted or self.updated or self.deleted)


def source_filter(source_table: str | None, source_id: int | None) -> list[ColumnElement[bool]]:
    if source_table is None:
        return []
    if source_table not in SOURCES:
        raise ValueError(f"unknown cash statement source {source_table!r}")
    return [CashStatement.source_table == source_table, CashStatement.source_id == source_id]


def plan_sync(db: Session, source_table: str | None = None, source_id: int | None = None) -> SyncPlan:
    """Compares the expected rows with the stored ones inside PostgreSQL (FULL OUTER JOIN) and
    returns only the counts, so nothing but the numbers is loaded into Python."""
    expected = expected_statements(source_table, source_id).subquery("expected")
    existing = select(CashStatement).where(*source_filter(source_table, source_id)).subquery("existing")
    key_match = and_(*(expected.c[name] == existing.c[name] for name in KEY_COLUMNS))
    differs = or_(*(expected.c[name].is_distinct_from(existing.c[name]) for name in VALUE_COLUMNS))
    inserted, updated, deleted = db.execute(
        select(
            func.count().filter(existing.c.id.is_(None)),
            func.count().filter(existing.c.id.is_not(None), expected.c.source_table.is_not(None), differs),
            func.count().filter(expected.c.source_table.is_(None)),
        ).select_from(expected.outerjoin(existing, key_match, full=True))
    ).one()
    return SyncPlan(int(inserted), int(updated), int(deleted))


def apply_sync(db: Session, source_table: str | None = None, source_id: int | None = None) -> None:
    """Upserts the expected rows (touching only those whose values changed) and deletes the stored
    rows that are no longer expected. Set-based: two statements whatever the row count."""
    expected = expected_statements(source_table, source_id)
    upsert = insert(CashStatement).from_select(list(COLUMNS), expected)
    db.execute(
        upsert.on_conflict_do_update(
            index_elements=list(KEY_COLUMNS),
            set_={name: upsert.excluded[name] for name in VALUE_COLUMNS} | {"updated_at": func.now()},
            where=or_(
                *(getattr(CashStatement, name).is_distinct_from(upsert.excluded[name]) for name in VALUE_COLUMNS)
            ),
        )
    )
    still_expected = expected.subquery("expected")
    db.execute(
        delete(CashStatement)
        .where(*source_filter(source_table, source_id))
        .where(~exists().where(*(still_expected.c[name] == getattr(CashStatement, name) for name in KEY_COLUMNS)))
    )


def sync_cash_statements(db: Session, source_table: str, source_id: int) -> None:
    """Brings the statement rows of one source transaction up to date, in the caller's transaction.

    Call it after every write that can move money: cashflow/revenue create, update, correction,
    decision, payment and void, and manual main account adjustments. It only writes the rows of
    that source, so it is cheap and never conflicts with the sync of another source. The session
    is flushed first so the rules see the pending changes.
    """
    db.flush()
    apply_sync(db, source_table, source_id)


@dataclass(frozen=True)
class SourceTotal:
    source_table: str
    rows: int
    credit: Decimal
    debit: Decimal

    @property
    def net(self) -> Decimal:
        return self.credit - self.debit


def source_totals(db: Session) -> list[SourceTotal]:
    """Row count, total credit and total debit per source table, in ``SOURCES`` order."""
    rows = db.execute(
        select(
            CashStatement.source_table,
            func.count(),
            func.coalesce(func.sum(CashStatement.amount).filter(CashStatement.direction == CREDIT), Decimal("0")),
            func.coalesce(func.sum(CashStatement.amount).filter(CashStatement.direction == DEBIT), Decimal("0")),
        ).group_by(CashStatement.source_table)
    ).all()
    found = {
        table: SourceTotal(table, int(count), Decimal(credit), Decimal(debit)) for table, count, credit, debit in rows
    }
    return [found.get(table, SourceTotal(table, 0, Decimal("0"), Decimal("0"))) for table in SOURCES]


@dataclass(frozen=True)
class RebuildResult:
    plan: SyncPlan
    totals: list[SourceTotal]
    dry_run: bool


def rebuild_cash_statements(db: Session, *, dry_run: bool = False) -> RebuildResult:
    """Populates/repairs the whole table from every source (the backfill command).

    Takes the same transactional lock as cashflow/revenue writes so it sees a consistent picture
    while the API is live. Commits unless ``dry_run`` (then rolls back after reporting what would
    change and the totals the table would have).
    """
    try:
        lock_balances(db)
        plan = plan_sync(db)
        apply_sync(db)
        totals = source_totals(db)
        if dry_run:
            db.rollback()
        else:
            db.commit()
        return RebuildResult(plan, totals, dry_run)
    except Exception:
        db.rollback()
        raise
