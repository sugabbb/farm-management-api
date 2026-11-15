from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
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
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin


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
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    species: Mapped[str] = mapped_column(String(10), index=True)
    code: Mapped[str] = mapped_column(String(60), unique=True)
    name: Mapped[str] = mapped_column(String(255))
    start_date: Mapped[date] = mapped_column(Date, index=True)
    initial_population: Mapped[int] = mapped_column(BigInteger)
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
