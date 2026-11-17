import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Computed,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin

# A log reference is "<source table>:<source id>:<action>" for automatic entries and "manual:<note>"
# otherwise; these expressions extract the source of an entry inside the database.
SOURCE_REFERENCE_PATTERN = "^(operational_cashflows|revenues):[0-9]+:"
SOURCE_TABLE_SQL = f"CASE WHEN reference ~ '{SOURCE_REFERENCE_PATTERN}' THEN split_part(reference, ':', 1) END"
SOURCE_ID_SQL = f"CASE WHEN reference ~ '{SOURCE_REFERENCE_PATTERN}' THEN split_part(reference, ':', 2)::bigint END"


class User(TimestampMixin, Base):
    __tablename__ = "users"
    __table_args__ = (CheckConstraint("role IN ('ADMIN','SUPERADMIN')", name="role"),)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(Text)
    role: Mapped[str] = mapped_column(String(20), default="ADMIN", index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=True)
    token_version: Mapped[int] = mapped_column(Integer, default=0)


class AuthLoginAttempt(Base):
    __tablename__ = "auth_login_attempts"
    key_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Employee(TimestampMixin, Base):
    __tablename__ = "employees"
    __table_args__ = (CheckConstraint("status IN ('ACTIVE','INACTIVE')", name="status"),)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    job_title: Mapped[str | None] = mapped_column(String(255))
    joined_at: Mapped[date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(20), default="ACTIVE", index=True)
    notes: Mapped[str | None] = mapped_column(Text)


class LivestockGroup(TimestampMixin, Base):
    __tablename__ = "livestock_groups"
    __table_args__ = (
        CheckConstraint("species IN ('AYAM','LELE')", name="species"),
        CheckConstraint("status IN ('ACTIVE','CLOSED')", name="status"),
        CheckConstraint(
            "initial_weight_g IS NULL OR (species = 'LELE' AND initial_weight_g > 0)", name="initial_weight"
        ),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    species: Mapped[str] = mapped_column(String(10), index=True)
    code: Mapped[str] = mapped_column(String(60), unique=True)
    name: Mapped[str] = mapped_column(String(255))
    start_date: Mapped[date] = mapped_column(Date, index=True)
    initial_population: Mapped[int] = mapped_column(BigInteger)
    # Lele only: average weight of one fish (grams) on start_date. When set, it is the starting
    # point of the group's first real ADG interval (stocking -> first sampling).
    initial_weight_g: Mapped[Decimal | None] = mapped_column(Numeric(12, 3))
    location: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(20), default="ACTIVE", index=True)
    closed_at: Mapped[date | None] = mapped_column(Date)
    closure_reason: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
    daily_records: Mapped[list[DailyRecord]] = relationship(back_populates="group")


class FeedType(TimestampMixin, Base):
    __tablename__ = "feed_types"
    __table_args__ = (
        CheckConstraint("species IN ('AYAM','LELE')", name="species"),
        UniqueConstraint("species", "name"),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    species: Mapped[str] = mapped_column(String(10), index=True)
    name: Mapped[str] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)


class DailyRecord(TimestampMixin, Base):
    __tablename__ = "daily_records"
    __table_args__ = (UniqueConstraint("group_id", "record_date"),)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    group_id: Mapped[int] = mapped_column(ForeignKey("livestock_groups.id", ondelete="RESTRICT"))
    record_date: Mapped[date] = mapped_column(Date, index=True)
    mortality: Mapped[int] = mapped_column(BigInteger, default=0)
    notes: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    group: Mapped[LivestockGroup] = relationship(back_populates="daily_records")
    feeds: Mapped[list[FeedUsage]] = relationship(cascade="all, delete-orphan")
    eggs: Mapped[list[EggHarvest]] = relationship(cascade="all, delete-orphan")


class FeedUsage(TimestampMixin, Base):
    __tablename__ = "feed_usages"
    __table_args__ = (
        UniqueConstraint("daily_record_id", "feed_type_id"),
        CheckConstraint(
            "morning_kg >= 0 AND noon_kg >= 0 AND evening_kg >= 0 AND quantity_kg >= 0 "
            "AND quantity_kg = morning_kg + noon_kg + evening_kg",
            name="quantities",
        ),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    daily_record_id: Mapped[int] = mapped_column(ForeignKey("daily_records.id", ondelete="CASCADE"))
    feed_type_id: Mapped[int] = mapped_column(ForeignKey("feed_types.id", ondelete="RESTRICT"))
    morning_kg: Mapped[Decimal] = mapped_column(Numeric(14, 3), default=0)
    noon_kg: Mapped[Decimal] = mapped_column(Numeric(14, 3), default=0)
    evening_kg: Mapped[Decimal] = mapped_column(Numeric(14, 3), default=0)
    quantity_kg: Mapped[Decimal] = mapped_column(Numeric(14, 3))
    feed_type: Mapped[FeedType] = relationship()


class EggHarvest(TimestampMixin, Base):
    __tablename__ = "egg_harvests"
    __table_args__ = (
        UniqueConstraint("daily_record_id", "egg_category"),
        CheckConstraint("egg_category IN ('UTUH','PECAH','REJECT')", name="category"),
        CheckConstraint("weight_kg >= 0 AND quantity_pieces >= 0", name="quantities"),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    daily_record_id: Mapped[int] = mapped_column(ForeignKey("daily_records.id", ondelete="CASCADE"))
    egg_category: Mapped[str] = mapped_column(String(10), index=True)
    weight_kg: Mapped[Decimal] = mapped_column(Numeric(14, 3), default=0)
    quantity_pieces: Mapped[int] = mapped_column(BigInteger, default=0)


class InventoryOpeningBalance(TimestampMixin, Base):
    __tablename__ = "inventory_opening_balances"
    __table_args__ = (
        CheckConstraint(
            "(item_kind='FEED' AND feed_type_id IS NOT NULL AND egg_category IS NULL AND quantity_pieces IS NULL) OR "
            "(item_kind='EGG' AND feed_type_id IS NULL AND egg_category IN ('UTUH','PECAH','REJECT') AND quantity_pieces IS NOT NULL)",
            name="shape",
        ),
        Index(
            "uq_inventory_opening_feed", "feed_type_id", unique=True, postgresql_where=text("feed_type_id IS NOT NULL")
        ),
        Index(
            "uq_inventory_opening_egg", "egg_category", unique=True, postgresql_where=text("egg_category IS NOT NULL")
        ),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    item_kind: Mapped[str] = mapped_column(String(10), index=True)
    feed_type_id: Mapped[int | None] = mapped_column(ForeignKey("feed_types.id", ondelete="RESTRICT"))
    egg_category: Mapped[str | None] = mapped_column(String(10), index=True)
    quantity_kg: Mapped[Decimal] = mapped_column(Numeric(14, 3), default=0)
    quantity_pieces: Mapped[int | None] = mapped_column(BigInteger)
    effective_date: Mapped[date] = mapped_column(Date, index=True)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))


class InventoryAdjustment(TimestampMixin, Base):
    __tablename__ = "inventory_adjustments"
    __table_args__ = (
        CheckConstraint(
            "(item_kind='FEED' AND feed_type_id IS NOT NULL AND egg_category IS NULL AND delta_pieces IS NULL) OR "
            "(item_kind='EGG' AND feed_type_id IS NULL AND egg_category IN ('UTUH','PECAH','REJECT') AND delta_pieces IS NOT NULL)",
            name="shape",
        ),
        CheckConstraint("delta_kg <> 0 OR COALESCE(delta_pieces,0) <> 0", name="nonzero"),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    item_kind: Mapped[str] = mapped_column(String(10), index=True)
    feed_type_id: Mapped[int | None] = mapped_column(ForeignKey("feed_types.id", ondelete="RESTRICT"))
    egg_category: Mapped[str | None] = mapped_column(String(10), index=True)
    delta_kg: Mapped[Decimal] = mapped_column(Numeric(14, 3))
    delta_pieces: Mapped[int | None] = mapped_column(BigInteger)
    effective_date: Mapped[date] = mapped_column(Date, index=True)
    reason: Mapped[str] = mapped_column(Text)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))


class PopulationAdjustment(TimestampMixin, Base):
    __tablename__ = "population_adjustments"
    __table_args__ = (CheckConstraint("delta_ekor <> 0", name="nonzero"),)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    group_id: Mapped[int] = mapped_column(ForeignKey("livestock_groups.id", ondelete="RESTRICT"))
    delta_ekor: Mapped[int] = mapped_column(BigInteger)
    effective_date: Mapped[date] = mapped_column(Date, index=True)
    reason: Mapped[str] = mapped_column(Text)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))


class FinancialOpeningBalance(TimestampMixin, Base):
    __tablename__ = "financial_opening_balances"
    __table_args__ = (
        CheckConstraint("account IN ('OPERATIONAL','REVENUE')", name="account"),
        CheckConstraint("amount_idr >= 0", name="amount"),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    account: Mapped[str] = mapped_column(String(20), unique=True, index=True)
    amount_idr: Mapped[Decimal] = mapped_column(Numeric(18, 0))
    effective_date: Mapped[date] = mapped_column(Date, index=True)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))


class Customer(TimestampMixin, Base):
    __tablename__ = "customers"
    __table_args__ = (CheckConstraint("customer_type IN ('WARUNG','AGEN')", name="type"),)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), index=True)
    phone: Mapped[str] = mapped_column(String(50), index=True)
    customer_type: Mapped[str] = mapped_column(String(20), index=True)
    notes: Mapped[str | None] = mapped_column(Text)


class OperationalCashflow(TimestampMixin, Base):
    __tablename__ = "operational_cashflows"
    __table_args__ = (
        CheckConstraint(
            "cashflow_type IN ('FEED_AYAM','FEED_LELE','SALARY','UTILITY','HEALTH','OTHER','LELE_SEED_PURCHASE','PULLET_PURCHASE','BANK_ADMIN_FEE','MAIN_ACCOUNT_TOPUP')",
            name="type",
        ),
        CheckConstraint(
            "(cashflow_type='MAIN_ACCOUNT_TOPUP' AND direction='INFLOW') OR (cashflow_type<>'MAIN_ACCOUNT_TOPUP' AND direction='OUTFLOW')",
            name="direction",
        ),
        CheckConstraint(
            "approval_status IN ('NOT_REQUIRED','DRAFT','SUBMITTED','APPROVED','REJECTED')", name="approval"
        ),
        CheckConstraint("payment_status IN ('UNPAID','PARTIALLY_PAID','PAID')", name="payment"),
        CheckConstraint("amount_idr >= 0", name="amount"),
        # Restrict the main cash account audit to top-ups instead of the whole table. The predicate
        # covers only the type so rows that stop qualifying (e.g. unpaid again) stay visible to it.
        Index(
            "ix_operational_cashflows_main_topup",
            "id",
            postgresql_where=text("cashflow_type = 'MAIN_ACCOUNT_TOPUP'"),
        ),
        Index(
            "ix_operational_cashflows_main_topup_updated",
            "updated_at",
            "id",
            postgresql_where=text("cashflow_type = 'MAIN_ACCOUNT_TOPUP'"),
        ),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    cashflow_type: Mapped[str] = mapped_column(String(40), index=True)
    direction: Mapped[str] = mapped_column(String(10), index=True)
    transaction_date: Mapped[date] = mapped_column(Date, index=True)
    amount_idr: Mapped[Decimal] = mapped_column(Numeric(18, 0))
    approval_status: Mapped[str] = mapped_column(String(20), default="DRAFT", index=True)
    payment_status: Mapped[str] = mapped_column(String(20), default="UNPAID", index=True)
    rejection_reason: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    submitted_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    approved_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    rejected_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    paid_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    proof_path: Mapped[str | None] = mapped_column(Text)
    proof_url: Mapped[str | None] = mapped_column(Text)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rejected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    feed_details: Mapped[list[OperationalCashflowFeedDetail]] = relationship(cascade="all, delete-orphan")
    payments: Mapped[list[OperationalCashflowPayment]] = relationship(cascade="all, delete-orphan")


class OperationalCashflowFeedDetail(TimestampMixin, Base):
    __tablename__ = "operational_cashflow_feed_details"
    __table_args__ = (
        CheckConstraint("purchase_unit IN ('KG','SAK')", name="unit"),
        CheckConstraint("quantity_kg >= 0 AND price_per_kg_idr >= 0 AND line_amount_idr >= 0", name="amounts"),
        CheckConstraint(
            "(purchase_unit='KG' AND purchase_quantity >= 0 AND kg_per_sak IS NULL) OR (purchase_unit='SAK' AND purchase_quantity > 0 AND purchase_quantity = TRUNC(purchase_quantity) AND kg_per_sak > 0)",
            name="shape",
        ),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    operational_cashflow_id: Mapped[int] = mapped_column(ForeignKey("operational_cashflows.id", ondelete="CASCADE"))
    feed_type_id: Mapped[int] = mapped_column(ForeignKey("feed_types.id", ondelete="RESTRICT"))
    purchase_unit: Mapped[str] = mapped_column(String(10))
    purchase_quantity: Mapped[Decimal] = mapped_column(Numeric(14, 3))
    kg_per_sak: Mapped[Decimal | None] = mapped_column(Numeric(14, 3))
    price_per_purchase_unit_idr: Mapped[Decimal] = mapped_column(Numeric(18, 3))
    quantity_kg: Mapped[Decimal] = mapped_column(Numeric(14, 3))
    price_per_kg_idr: Mapped[Decimal] = mapped_column(Numeric(18, 3))
    line_amount_idr: Mapped[Decimal] = mapped_column(Numeric(18, 0))
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    received_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    feed_type: Mapped[FeedType] = relationship()


class _OpDetailMixin(TimestampMixin):
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    operational_cashflow_id: Mapped[int] = mapped_column(
        ForeignKey("operational_cashflows.id", ondelete="CASCADE"), unique=True
    )


class OperationalCashflowSalaryDetail(_OpDetailMixin, Base):
    __tablename__ = "operational_cashflow_salary_details"
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id", ondelete="RESTRICT"))
    salary_period: Mapped[str] = mapped_column(String(40))


class OperationalCashflowUtilityDetail(_OpDetailMixin, Base):
    __tablename__ = "operational_cashflow_utility_details"
    description: Mapped[str] = mapped_column(Text)


class OperationalCashflowHealthDetail(_OpDetailMixin, Base):
    __tablename__ = "operational_cashflow_health_details"
    __table_args__ = (CheckConstraint("species IN ('AYAM','LELE')", name="species"),)
    species: Mapped[str] = mapped_column(String(10))
    description: Mapped[str] = mapped_column(Text)


class OperationalCashflowOtherDetail(_OpDetailMixin, Base):
    __tablename__ = "operational_cashflow_other_details"
    description: Mapped[str] = mapped_column(Text)


class OperationalCashflowTopupDetail(_OpDetailMixin, Base):
    __tablename__ = "operational_cashflow_topup_details"
    notes: Mapped[str | None] = mapped_column(Text)


class OperationalCashflowLivestockPurchaseDetail(_OpDetailMixin, Base):
    __tablename__ = "operational_cashflow_livestock_purchase_details"
    __table_args__ = (CheckConstraint("species IN ('AYAM','LELE')", name="species"),)
    species: Mapped[str] = mapped_column(String(10), index=True)
    notes: Mapped[str | None] = mapped_column(Text)


class OperationalCashflowBankAdminDetail(_OpDetailMixin, Base):
    __tablename__ = "operational_cashflow_bank_admin_details"
    notes: Mapped[str | None] = mapped_column(Text)


class OperationalCashflowPayment(TimestampMixin, Base):
    __tablename__ = "operational_cashflow_payments"
    __table_args__ = (
        CheckConstraint("payment_mode IN ('PARTIAL','FULL')", name="mode"),
        CheckConstraint("amount_idr >= 0", name="amount"),
        CheckConstraint(
            "(voided_at IS NULL AND voided_by IS NULL AND void_reason IS NULL) OR (voided_at IS NOT NULL AND voided_by IS NOT NULL AND void_reason IS NOT NULL)",
            name="void",
        ),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    operational_cashflow_id: Mapped[int] = mapped_column(
        ForeignKey("operational_cashflows.id", ondelete="CASCADE"), index=True
    )
    paid_date: Mapped[date] = mapped_column(Date, index=True)
    amount_idr: Mapped[Decimal] = mapped_column(Numeric(18, 0))
    payment_mode: Mapped[str] = mapped_column(String(20))
    proof_path: Mapped[str | None] = mapped_column(Text)
    proof_url: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    voided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    voided_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    void_reason: Mapped[str | None] = mapped_column(Text)


class OperationalCashflowBalance(Base):
    __tablename__ = "operational_cashflow_balances"
    __table_args__ = (CheckConstraint("type IN ('cash_on_hand','waiting_approval','approved_unpaid')", name="type"),)
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    type: Mapped[str] = mapped_column(String(30), unique=True, index=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 0), default=Decimal("0"), server_default="0")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class MainCashAccount(Base):
    """The single main cash account. Exactly one row may exist (enforced by the singleton index)."""

    __tablename__ = "main_cash_accounts"
    __table_args__ = (Index("uq_main_cash_accounts_singleton", text("(true)"), unique=True),)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    current_amount: Mapped[Decimal] = mapped_column(Numeric(18, 0), default=Decimal("0"), server_default="0")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class MainCashAccountLog(Base):
    """Immutable ledger of every movement of the main cash account. ``credit`` adds to
    ``current_amount`` and ``debit`` subtracts from it, so the signed sum of the log always equals
    the account's ``current_amount``.
    """

    __tablename__ = "main_cash_account_logs"
    __table_args__ = (
        CheckConstraint("direction IN ('credit','debit')", name="direction"),
        CheckConstraint("amount > 0", name="amount"),
        # Lets the audit look up "what has already been logged for this source row".
        Index(
            "ix_main_cash_account_logs_source",
            "source_table",
            "source_id",
            postgresql_where=text("source_table IS NOT NULL"),
        ),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    main_cash_account_id: Mapped[int] = mapped_column(
        ForeignKey("main_cash_accounts.id", ondelete="RESTRICT"), index=True
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 0))
    direction: Mapped[str] = mapped_column(String(10), index=True)
    reference: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    # Derived from ``reference`` by the database so the audit can join and index the source of an
    # entry without parsing text per row. NULL for manual adjustments.
    source_table: Mapped[str | None] = mapped_column(Text, Computed(SOURCE_TABLE_SQL, persisted=True))
    source_id: Mapped[int | None] = mapped_column(BigInteger, Computed(SOURCE_ID_SQL, persisted=True))


class MainCashAccountEvent(Base):
    """Outbox of main cash account movements that are committed but not posted to the log yet.

    Cashflow/revenue transactions only insert a row here (no lock on the account row); a
    background worker turns each row into a ``main_cash_account_logs`` entry, moves
    ``current_amount`` and deletes the row, so the table only ever holds the backlog.
    """

    __tablename__ = "main_cash_account_events"
    __table_args__ = (
        CheckConstraint("source_table IN ('operational_cashflows','revenues')", name="source"),
        CheckConstraint("delta <> 0", name="delta"),
        Index("ix_main_cash_account_events_source", "source_table", "source_id"),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    source_table: Mapped[str] = mapped_column(String(40))
    source_id: Mapped[int] = mapped_column(BigInteger)
    action: Mapped[str] = mapped_column(String(100))
    # Signed: positive = credit, negative = debit.
    delta: Mapped[Decimal] = mapped_column(Numeric(18, 0))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class MainCashAccountAuditRun(Base):
    """One execution of the daily main cash account audit (see app/scripts/audit_main_cash_account.py).

    The most recent successful run is also the watermark an incremental run starts from, and its
    ``log_high_id``/``new_amount`` let the next run add up only the log entries written since.
    """

    __tablename__ = "main_cash_account_audit_runs"
    __table_args__ = (
        CheckConstraint("mode IN ('full','incremental')", name="mode"),
        CheckConstraint("status IN ('RUNNING','SUCCESS','FAILED','SKIPPED')", name="status"),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    mode: Mapped[str] = mapped_column(String(20), index=True)
    status: Mapped[str] = mapped_column(String(20), index=True)
    dry_run: Mapped[bool] = mapped_column(Boolean, default=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    window_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sources_checked: Mapped[int] = mapped_column(BigInteger, default=0)
    batches: Mapped[int] = mapped_column(Integer, default=0)
    discrepancies: Mapped[int] = mapped_column(BigInteger, default=0)
    entries_posted: Mapped[int] = mapped_column(BigInteger, default=0)
    # Pending outbox events the run posted before auditing (normally 0: the worker keeps up).
    events_processed: Mapped[int] = mapped_column(BigInteger, default=0)
    credit_posted: Mapped[Decimal] = mapped_column(Numeric(18, 0), default=Decimal("0"))
    debit_posted: Mapped[Decimal] = mapped_column(Numeric(18, 0), default=Decimal("0"))
    previous_amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 0))
    new_amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 0))
    log_high_id: Mapped[int | None] = mapped_column(BigInteger)
    error: Mapped[str | None] = mapped_column(Text)


class Revenue(TimestampMixin, Base):
    __tablename__ = "revenues"
    __table_args__ = (
        CheckConstraint("revenue_type IN ('EGG_SALE','FISH_SALE','OTHER','MAIN_ACCOUNT_TRANSFER')", name="type"),
        CheckConstraint(
            "(revenue_type='MAIN_ACCOUNT_TRANSFER' AND direction='OUTFLOW') OR (revenue_type<>'MAIN_ACCOUNT_TRANSFER' AND direction='INFLOW')",
            name="direction",
        ),
        CheckConstraint(
            "approval_status IN ('NOT_REQUIRED','DRAFT','SUBMITTED','APPROVED','REJECTED')", name="approval"
        ),
        CheckConstraint("payment_status IN ('UNPAID','PARTIALLY_PAID','PAID')", name="payment"),
        CheckConstraint("amount_idr >= 0", name="amount"),
        # Same idea as the top-up indexes above, for main account transfers.
        Index(
            "ix_revenues_main_transfer",
            "id",
            postgresql_where=text("revenue_type = 'MAIN_ACCOUNT_TRANSFER'"),
        ),
        Index(
            "ix_revenues_main_transfer_updated",
            "updated_at",
            "id",
            postgresql_where=text("revenue_type = 'MAIN_ACCOUNT_TRANSFER'"),
        ),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    revenue_type: Mapped[str] = mapped_column(String(30), index=True)
    direction: Mapped[str] = mapped_column(String(10), index=True)
    transaction_date: Mapped[date] = mapped_column(Date, index=True)
    amount_idr: Mapped[Decimal] = mapped_column(Numeric(18, 0))
    approval_status: Mapped[str] = mapped_column(String(20), default="NOT_REQUIRED", index=True)
    payment_status: Mapped[str] = mapped_column(String(20), default="UNPAID", index=True)
    payment_method: Mapped[str | None] = mapped_column(String(20), index=True)
    direct_transfer_date: Mapped[date | None] = mapped_column(Date)
    customer_id: Mapped[int | None] = mapped_column(ForeignKey("customers.id", ondelete="RESTRICT"))
    rejection_reason: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    submitted_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    approved_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    rejected_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    paid_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    proof_path: Mapped[str | None] = mapped_column(Text)
    proof_url: Mapped[str | None] = mapped_column(Text)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rejected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    customer: Mapped[Customer | None] = relationship()
    egg_details: Mapped[list[RevenueEggDetail]] = relationship(cascade="all, delete-orphan")
    fish_detail: Mapped[RevenueFishDetail | None] = relationship(cascade="all, delete-orphan")
    payments: Mapped[list[RevenuePayment]] = relationship(cascade="all, delete-orphan")


class RevenueEggDetail(TimestampMixin, Base):
    __tablename__ = "revenue_egg_details"
    __table_args__ = (
        UniqueConstraint("revenue_id", "egg_category"),
        CheckConstraint("egg_category IN ('UTUH','PECAH','REJECT')", name="category"),
        CheckConstraint(
            "weight_kg >= 0 AND quantity_pieces >= 0 AND price_per_kg_idr >= 0 AND line_amount_idr >= 0", name="amounts"
        ),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    revenue_id: Mapped[int] = mapped_column(ForeignKey("revenues.id", ondelete="CASCADE"))
    egg_category: Mapped[str] = mapped_column(String(10), index=True)
    weight_kg: Mapped[Decimal] = mapped_column(Numeric(14, 3))
    quantity_pieces: Mapped[int] = mapped_column(BigInteger)
    price_per_kg_idr: Mapped[Decimal] = mapped_column(Numeric(18, 3))
    line_amount_idr: Mapped[Decimal] = mapped_column(Numeric(18, 0))
    notes: Mapped[str | None] = mapped_column(Text)


class RevenueFishDetail(TimestampMixin, Base):
    __tablename__ = "revenue_fish_details"
    __table_args__ = (CheckConstraint("weight_kg >= 0 AND price_per_kg_idr >= 0", name="amounts"),)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    revenue_id: Mapped[int] = mapped_column(ForeignKey("revenues.id", ondelete="CASCADE"), unique=True)
    weight_kg: Mapped[Decimal] = mapped_column(Numeric(14, 3))
    price_per_kg_idr: Mapped[Decimal] = mapped_column(Numeric(18, 3))
    notes: Mapped[str | None] = mapped_column(Text)


class RevenueOtherDetail(TimestampMixin, Base):
    __tablename__ = "revenue_other_details"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    revenue_id: Mapped[int] = mapped_column(ForeignKey("revenues.id", ondelete="CASCADE"), unique=True)
    notes: Mapped[str | None] = mapped_column(Text)


class RevenueTransferDetail(TimestampMixin, Base):
    __tablename__ = "revenue_transfer_details"
    __table_args__ = (
        CheckConstraint(
            "gross_amount_idr >= 0 AND payment_cost_idr >= 0 AND payment_cost_idr <= gross_amount_idr", name="amounts"
        ),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    revenue_id: Mapped[int] = mapped_column(ForeignKey("revenues.id", ondelete="CASCADE"), unique=True)
    gross_amount_idr: Mapped[Decimal] = mapped_column(Numeric(18, 0))
    payment_cost_idr: Mapped[Decimal] = mapped_column(Numeric(18, 0))
    notes: Mapped[str | None] = mapped_column(Text)


class RevenuePayment(TimestampMixin, Base):
    __tablename__ = "revenue_payments"
    __table_args__ = (
        CheckConstraint("payment_mode IN ('PARTIAL','FULL')", name="mode"),
        CheckConstraint("amount_idr >= 0", name="amount"),
        CheckConstraint(
            "(voided_at IS NULL AND voided_by IS NULL AND void_reason IS NULL) OR (voided_at IS NOT NULL AND voided_by IS NOT NULL AND void_reason IS NOT NULL)",
            name="void",
        ),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    revenue_id: Mapped[int] = mapped_column(ForeignKey("revenues.id", ondelete="CASCADE"), index=True)
    paid_date: Mapped[date] = mapped_column(Date, index=True)
    amount_idr: Mapped[Decimal] = mapped_column(Numeric(18, 0))
    payment_mode: Mapped[str] = mapped_column(String(20))
    proof_path: Mapped[str | None] = mapped_column(Text)
    proof_url: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    voided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    voided_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    void_reason: Mapped[str | None] = mapped_column(Text)


class LeleSampling(TimestampMixin, Base):
    __tablename__ = "lele_samplings"
    __table_args__ = (UniqueConstraint("group_id", "sampling_date"),)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    group_id: Mapped[int] = mapped_column(ForeignKey("livestock_groups.id", ondelete="RESTRICT"))
    sampling_date: Mapped[date] = mapped_column(Date, index=True)
    notes: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    group: Mapped[LivestockGroup] = relationship()
    measurements: Mapped[list[LeleSamplingMeasurement]] = relationship(cascade="all, delete-orphan")
    result: Mapped[LeleSamplingResult | None] = relationship(
        cascade="all, delete-orphan", foreign_keys="LeleSamplingResult.sampling_id"
    )


class LeleSamplingMeasurement(TimestampMixin, Base):
    __tablename__ = "lele_sampling_measurements"
    __table_args__ = (
        UniqueConstraint("sampling_id", "sequence"),
        CheckConstraint("sequence BETWEEN 1 AND 3", name="sequence"),
        CheckConstraint("fish_count = 10", name="fish_count"),
        CheckConstraint("total_weight_kg > 0", name="weight"),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    sampling_id: Mapped[int] = mapped_column(ForeignKey("lele_samplings.id", ondelete="CASCADE"))
    sequence: Mapped[int] = mapped_column(SmallInteger)
    fish_count: Mapped[int] = mapped_column(SmallInteger, default=10)
    total_weight_kg: Mapped[Decimal] = mapped_column(Numeric(14, 3))


class LeleSamplingResult(TimestampMixin, Base):
    """Derived per-fish weight and real ADG of one ``lele_samplings`` row (see lele_biomass_service).

    Never written directly: every sampling create/update (and every change of the group's
    ``initial_weight_g``) recomputes the rows of the whole group, so a backdated or edited sampling
    also refreshes the ADG of the sampling after it. The ADG covers the interval ending at this
    sampling and starting at:

    - ``previous_sampling_id``, the group's preceding sampling; or, for the first sampling,
    - the stocking (``previous_sampling_id`` NULL): the group's ``start_date`` and
      ``initial_weight_g``. The ADG is NULL when the group has no initial weight or the first
      sampling is on the start date.
    """

    __tablename__ = "lele_sampling_results"
    __table_args__ = (
        CheckConstraint("fish_count > 0 AND total_weight_kg > 0 AND average_weight_g > 0", name="weights"),
        CheckConstraint(
            "(days_since_previous IS NULL AND weight_gain_g IS NULL AND adg_g IS NULL)"
            " OR (days_since_previous > 0 AND weight_gain_g IS NOT NULL AND adg_g IS NOT NULL)",
            name="interval",
        ),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    sampling_id: Mapped[int] = mapped_column(ForeignKey("lele_samplings.id", ondelete="CASCADE"), unique=True)
    fish_count: Mapped[int] = mapped_column(Integer)
    total_weight_kg: Mapped[Decimal] = mapped_column(Numeric(14, 3))
    # Average weight of one fish, in grams: total_weight_kg * 1000 / fish_count.
    average_weight_g: Mapped[Decimal] = mapped_column(Numeric(12, 3))
    previous_sampling_id: Mapped[int | None] = mapped_column(
        ForeignKey("lele_samplings.id", ondelete="SET NULL"), index=True
    )
    days_since_previous: Mapped[int | None] = mapped_column(Integer)
    # May be negative: sampling 30 fish is noisy, and the data is shown as measured.
    weight_gain_g: Mapped[Decimal | None] = mapped_column(Numeric(12, 3))
    adg_g: Mapped[Decimal | None] = mapped_column(Numeric(12, 3))


class AuditEvent(TimestampMixin, Base):
    __tablename__ = "audit_events"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    actor_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    entity_type: Mapped[str] = mapped_column(String(100), index=True)
    entity_id: Mapped[int] = mapped_column(BigInteger, index=True)
    action: Mapped[str] = mapped_column(String(100), index=True)
    happened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    reason: Mapped[str | None] = mapped_column(Text)
    before: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    after: Mapped[dict[str, Any] | None] = mapped_column(JSONB)


# --- Lele biomass read model -----------------------------------------------------------------
# Precomputed by the background rebuild (app.features.lele_biomass_jobs) so that
# GET /lele-biomass/{group_id} only reads these tables. Never edited by hand: every row of a group
# is replaced by each rebuild.


class LeleBiomassJob(Base):
    """Outbox of Lele groups whose biomass read model must be rebuilt.

    Inserted in the same transaction as the change (sampling, initial weight, mortality, ...), so a
    committed change can never be missed; deleted once the group has been rebuilt. Several jobs of
    one group are rebuilt once.
    """

    __tablename__ = "lele_biomass_jobs"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    group_id: Mapped[int] = mapped_column(ForeignKey("livestock_groups.id", ondelete="CASCADE"), index=True)
    reason: Mapped[str] = mapped_column(String(60))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class LeleBiomassSnapshot(Base):
    """One row per Lele group: details, latest sampling and harvest prediction."""

    __tablename__ = "lele_biomass_snapshots"
    __table_args__ = (
        CheckConstraint("baseline_source IS NULL OR baseline_source IN ('SAMPLING','STOCKING')", name="baseline"),
        CheckConstraint("adg_source IS NULL OR adg_source IN ('ACTUAL','DEFAULT')", name="adg_source"),
        CheckConstraint(
            "(baseline_source IS NULL AND baseline_date IS NULL AND baseline_weight_g IS NULL AND adg_source IS NULL"
            " AND adg_g IS NULL AND days_to_target IS NULL AND harvest_date IS NULL AND ready_to_sell IS NULL"
            " AND remaining_weight_g IS NULL AND projected_biomass_kg IS NULL)"
            " OR (baseline_source IS NOT NULL AND baseline_date IS NOT NULL AND baseline_weight_g IS NOT NULL"
            " AND adg_source IS NOT NULL AND adg_g IS NOT NULL AND days_to_target IS NOT NULL"
            " AND harvest_date IS NOT NULL AND ready_to_sell IS NOT NULL AND remaining_weight_g IS NOT NULL"
            " AND projected_biomass_kg IS NOT NULL)",
            name="projection",
        ),
    )
    group_id: Mapped[int] = mapped_column(ForeignKey("livestock_groups.id", ondelete="CASCADE"), primary_key=True)
    # Group details, copied so the endpoint does not read livestock_groups.
    name: Mapped[str] = mapped_column(String(255))
    location: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(20))
    start_date: Mapped[date] = mapped_column(Date)
    closed_at: Mapped[date | None] = mapped_column(Date)
    initial_population: Mapped[int] = mapped_column(BigInteger)
    current_population: Mapped[int] = mapped_column(BigInteger)
    population_as_of: Mapped[date] = mapped_column(Date)
    initial_weight_g: Mapped[Decimal | None] = mapped_column(Numeric(12, 3))
    notes: Mapped[str | None] = mapped_column(Text)
    # Settings in force when the row was computed.
    target_weight_g: Mapped[Decimal] = mapped_column(Numeric(12, 3))
    default_adg_g: Mapped[Decimal] = mapped_column(Numeric(12, 3))
    # Latest sampling (NULL without samplings).
    latest_sampling_date: Mapped[date | None] = mapped_column(Date)
    latest_weight_g: Mapped[Decimal | None] = mapped_column(Numeric(12, 3))
    latest_population: Mapped[int | None] = mapped_column(BigInteger)
    latest_biomass_kg: Mapped[Decimal | None] = mapped_column(Numeric(18, 3))
    # Harvest prediction from the baseline (latest sampling, else stocking) with the latest real
    # ADG, or the default ADG when there is no positive real ADG. NULL without any known weight.
    baseline_source: Mapped[str | None] = mapped_column(String(10))
    baseline_date: Mapped[date | None] = mapped_column(Date)
    baseline_weight_g: Mapped[Decimal | None] = mapped_column(Numeric(12, 3))
    adg_source: Mapped[str | None] = mapped_column(String(10))
    adg_g: Mapped[Decimal | None] = mapped_column(Numeric(12, 3))
    days_to_target: Mapped[int | None] = mapped_column(Integer)
    harvest_date: Mapped[date | None] = mapped_column(Date)
    ready_to_sell: Mapped[bool | None] = mapped_column(Boolean)
    remaining_weight_g: Mapped[Decimal | None] = mapped_column(Numeric(12, 3))
    projected_biomass_kg: Mapped[Decimal | None] = mapped_column(Numeric(18, 3))
    # Chart bounds: the X axis ends on chart_end_date (the harvest date, capped); Y from 0 to y_max_g.
    chart_end_date: Mapped[date] = mapped_column(Date)
    y_max_g: Mapped[Decimal] = mapped_column(Numeric(12, 3))
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class LeleBiomassChartDate(Base):
    """X axis of a group's weight chart, with the measured weight/biomass on measured dates."""

    __tablename__ = "lele_biomass_chart_dates"
    group_id: Mapped[int] = mapped_column(
        ForeignKey("lele_biomass_snapshots.group_id", ondelete="CASCADE"), primary_key=True
    )
    chart_date: Mapped[date] = mapped_column(Date, primary_key=True)
    day: Mapped[int] = mapped_column(Integer)
    # Week number on the weekly dates; NULL on a sampling/harvest date between two weeks.
    week: Mapped[int | None] = mapped_column(Integer)
    is_sampling: Mapped[bool] = mapped_column(Boolean)
    is_harvest_date: Mapped[bool] = mapped_column(Boolean)
    # Measured on this date (stocking or sampling); NULL otherwise.
    actual_weight_g: Mapped[Decimal | None] = mapped_column(Numeric(12, 3))
    population: Mapped[int | None] = mapped_column(BigInteger)
    biomass_kg: Mapped[Decimal | None] = mapped_column(Numeric(18, 3))


class LeleBiomassChartSeries(Base):
    """One ADG line of a group's weight chart: the default ADG or a real ADG."""

    __tablename__ = "lele_biomass_chart_series"
    __table_args__ = (
        UniqueConstraint("group_id", "position"),
        CheckConstraint("type IN ('DEFAULT','REAL')", name="type"),
        CheckConstraint("period_from IS NULL OR period_from IN ('SAMPLING','STOCKING')", name="period_from"),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    group_id: Mapped[int] = mapped_column(ForeignKey("lele_biomass_snapshots.group_id", ondelete="CASCADE"), index=True)
    position: Mapped[int] = mapped_column(SmallInteger)
    name: Mapped[str] = mapped_column(String(60))
    type: Mapped[str] = mapped_column(String(10))
    is_latest: Mapped[bool] = mapped_column(Boolean)
    adg_g: Mapped[Decimal] = mapped_column(Numeric(12, 3))
    # Where the line starts, and when it reaches the target (NULL if it never does).
    start_date: Mapped[date | None] = mapped_column(Date)
    start_weight_g: Mapped[Decimal | None] = mapped_column(Numeric(12, 3))
    days_to_target: Mapped[int | None] = mapped_column(Integer)
    projected_sell_date: Mapped[date | None] = mapped_column(Date)
    # Real ADG only: the interval it was measured over.
    period_from: Mapped[str | None] = mapped_column(String(10))
    period_from_date: Mapped[date | None] = mapped_column(Date)
    period_to_date: Mapped[date | None] = mapped_column(Date)
    period_days: Mapped[int | None] = mapped_column(Integer)
    period_from_weight_g: Mapped[Decimal | None] = mapped_column(Numeric(12, 3))
    period_to_weight_g: Mapped[Decimal | None] = mapped_column(Numeric(12, 3))
    period_weight_gain_g: Mapped[Decimal | None] = mapped_column(Numeric(12, 3))


class LeleBiomassChartValue(Base):
    """Weight of one fish on one chart date for one ADG line (only the dates the line covers)."""

    __tablename__ = "lele_biomass_chart_values"
    series_id: Mapped[int] = mapped_column(
        ForeignKey("lele_biomass_chart_series.id", ondelete="CASCADE"), primary_key=True
    )
    chart_date: Mapped[date] = mapped_column(Date, primary_key=True)
    weight_g: Mapped[Decimal] = mapped_column(Numeric(12, 3))
