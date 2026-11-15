from datetime import date
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, status
from pydantic import Field, model_validator
from sqlalchemy import func, select

from app.core.decimal import money, quantity
from app.core.dependencies import ActiveUser, DbSession, Superadmin
from app.core.errors import conflict, not_found, validation
from app.core.pagination import StrictModel
from app.db.models import (
    FeedType,
    FinancialOpeningBalance,
    InventoryOpeningBalance,
    OperationalCashflow,
    OperationalCashflowPayment,
    Revenue,
    RevenuePayment,
)
from app.features.common import audit, model_snapshot
from app.features.farm_service import lock_balances

router = APIRouter(tags=["Settings"])


class FeedTypeCreate(StrictModel):
    species: Literal["AYAM", "LELE"]
    name: str = Field(min_length=1, max_length=255)


class FeedTypeUpdate(StrictModel):
    name: str = Field(min_length=1, max_length=255)
    is_active: bool


class FinancialOpeningInput(StrictModel):
    account: Literal["OPERATIONAL", "REVENUE"]
    amount_idr: Decimal = Field(ge=0)
    effective_date: date


class InventoryOpeningInput(StrictModel):
    item_kind: Literal["FEED", "EGG"]
    feed_type_id: int | None = None
    egg_category: Literal["UTUH", "PECAH", "REJECT"] | None = None
    quantity_kg: Decimal
    quantity_pieces: int | None = None
    effective_date: date

    @model_validator(mode="after")
    def shape(self) -> InventoryOpeningInput:
        if self.item_kind == "FEED" and (
            not self.feed_type_id or self.egg_category is not None or self.quantity_pieces is not None
        ):
            raise ValueError("FEED requires feed_type_id and prohibits egg fields")
        if self.item_kind == "EGG" and (
            not self.egg_category or self.feed_type_id is not None or self.quantity_pieces is None
        ):
            raise ValueError("EGG requires egg_category and quantity_pieces")
        return self


@router.get("/feed-types")
def feed_types(
    _: ActiveUser, db: DbSession, species: Literal["AYAM", "LELE"] | None = None, active_only: bool = False
) -> dict[str, object]:
    stmt = select(FeedType)
    if species:
        stmt = stmt.where(FeedType.species == species)
    if active_only:
        stmt = stmt.where(FeedType.is_active.is_(True))
    rows = db.scalars(stmt.order_by(FeedType.species, FeedType.name)).all()
    return {"data": [model_snapshot(row) for row in rows]}


@router.post("/feed-types", status_code=status.HTTP_201_CREATED)
def create_feed_type(payload: FeedTypeCreate, actor: Superadmin, db: DbSession) -> dict[str, object]:
    duplicate = db.scalar(
        select(FeedType.id).where(
            FeedType.species == payload.species, func.lower(FeedType.name) == payload.name.lower()
        )
    )
    if duplicate:
        raise conflict("FEED_TYPE_EXISTS", "Feed type already exists for this species.")
    row = FeedType(**payload.model_dump(), is_active=True)
    db.add(row)
    db.flush()
    audit(db, row, "feed_type_created", actor)
    db.commit()
    return {"data": model_snapshot(row)}


@router.patch("/feed-types/{feed_type_id}")
def update_feed_type(feed_type_id: int, payload: FeedTypeUpdate, actor: Superadmin, db: DbSession) -> dict[str, object]:
    row = db.get(FeedType, feed_type_id)
    if not row:
        raise not_found("Feed type")
    duplicate = db.scalar(
        select(FeedType.id).where(
            FeedType.species == row.species, func.lower(FeedType.name) == payload.name.lower(), FeedType.id != row.id
        )
    )
    if duplicate:
        raise conflict("FEED_TYPE_EXISTS", "Feed type already exists for this species.")
    before = model_snapshot(row)
    row.name, row.is_active = payload.name, payload.is_active
    audit(db, row, "feed_type_updated", actor, before=before)
    db.commit()
    return {"data": model_snapshot(row)}


@router.get("/settings/opening-balances")
def opening_balances(_: Superadmin, db: DbSession) -> dict[str, object]:
    financial = db.scalars(select(FinancialOpeningBalance).order_by(FinancialOpeningBalance.account)).all()
    inventory = db.scalars(
        select(InventoryOpeningBalance).order_by(InventoryOpeningBalance.item_kind, InventoryOpeningBalance.id)
    ).all()
    return {
        "data": {
            "financial": [model_snapshot(row) for row in financial],
            "inventory": [model_snapshot(row) for row in inventory],
        }
    }


@router.post("/settings/financial-opening-balances", status_code=status.HTTP_201_CREATED)
def create_financial_opening(payload: FinancialOpeningInput, actor: Superadmin, db: DbSession) -> dict[str, object]:
    if db.scalar(select(FinancialOpeningBalance.id).where(FinancialOpeningBalance.account == payload.account)):
        raise conflict("OPENING_EXISTS", "An opening balance already exists for this account.")
    if payload.account == "OPERATIONAL":
        first = min(
            filter(
                None,
                [
                    db.scalar(select(func.min(OperationalCashflow.paid_at))),
                    db.scalar(select(func.min(OperationalCashflowPayment.paid_date))),
                ],
            ),
            default=None,
        )
    else:
        first = min(
            filter(
                None,
                [db.scalar(select(func.min(Revenue.paid_at))), db.scalar(select(func.min(RevenuePayment.paid_date)))],
            ),
            default=None,
        )
    if first and payload.effective_date > (first.date() if hasattr(first, "date") else first):
        raise validation("effective_date", "Opening balance must start on or before the first payment.")
    row = FinancialOpeningBalance(
        account=payload.account,
        amount_idr=money(payload.amount_idr),
        effective_date=payload.effective_date,
        created_by=actor.id,
    )
    db.add(row)
    db.flush()
    audit(db, row, "financial_opening_created", actor)
    db.commit()
    return {"data": model_snapshot(row)}


@router.post("/settings/inventory-opening-balances", status_code=status.HTTP_201_CREATED)
def create_inventory_opening(payload: InventoryOpeningInput, actor: Superadmin, db: DbSession) -> dict[str, object]:
    lock_balances(db)
    if payload.item_kind == "FEED":
        if not db.get(FeedType, payload.feed_type_id):
            raise validation("feed_type_id", "Feed type does not exist.")
        exists = db.scalar(
            select(InventoryOpeningBalance.id).where(InventoryOpeningBalance.feed_type_id == payload.feed_type_id)
        )
    else:
        exists = db.scalar(
            select(InventoryOpeningBalance.id).where(InventoryOpeningBalance.egg_category == payload.egg_category)
        )
    if exists:
        raise conflict("OPENING_EXISTS", "An opening balance already exists for this item.")
    row = InventoryOpeningBalance(
        **payload.model_dump(), quantity_kg=quantity(payload.quantity_kg), created_by=actor.id
    )
    db.add(row)
    db.flush()
    audit(db, row, "inventory_opening_created", actor)
    db.commit()
    return {"data": model_snapshot(row)}
