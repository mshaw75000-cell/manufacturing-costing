"""Lot / heat register and traceability."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ..db import get_db
from ..events import broker
from ..models import Item, Lot
from ..services import trace
from . import serialize as S

router = APIRouter(prefix="/api")


class LotIn(BaseModel):
    item_id: int
    lot_no: str = Field(min_length=1)
    heat_no: str = ""
    qty: float = Field(gt=0)
    supplier: str = ""
    cert_ref: str = ""
    notes: str = ""


@router.get("/lots")
def list_lots(q: str = "", item_id: int | None = None, available: bool = False, limit: int = 200,
              db: Session = Depends(get_db)):
    stmt = select(Lot).join(Item).order_by(Lot.created_at.desc(), Lot.id.desc()).limit(limit)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(or_(Lot.lot_no.ilike(like), Lot.heat_no.ilike(like), Item.no.ilike(like),
                              Lot.cert_ref.ilike(like), Lot.supplier.ilike(like)))
    if item_id:
        stmt = stmt.where(Lot.item_id == item_id)
    if available:
        stmt = stmt.where(Lot.qty_on_hand > 1e-9)
    return [S.lot(l) for l in db.scalars(stmt)]


@router.post("/lots")
def receive_lot(body: LotIn, db: Session = Depends(get_db)):
    """Register a received lot (e.g. a steel delivery with its heat number and
    mill cert). Stock quantity itself comes from BC; this records which lots
    make up that stock."""
    item = db.get(Item, body.item_id)
    if not item:
        raise HTTPException(404, "Item not found")
    if db.scalar(select(Lot).where(Lot.item_id == item.id, Lot.lot_no == body.lot_no)):
        raise HTTPException(409, "That lot number already exists for this item")
    lot = Lot(item_id=item.id, lot_no=body.lot_no, heat_no=body.heat_no, supplier=body.supplier,
              cert_ref=body.cert_ref, notes=body.notes, qty_received=body.qty, qty_on_hand=body.qty)
    db.add(lot)
    db.commit()
    broker.publish("lots")
    return S.lot(lot)


@router.patch("/lots/{lot_id}")
def update_lot(lot_id: int, body: dict, db: Session = Depends(get_db)):
    lot = db.get(Lot, lot_id)
    if not lot:
        raise HTTPException(404, "Lot not found")
    for k in ("heat_no", "supplier", "cert_ref", "notes"):
        if k in body:
            setattr(lot, k, body[k])
    db.commit()
    return S.lot(lot)


@router.get("/lots/{lot_id}/trace")
def trace_lot(lot_id: int, db: Session = Depends(get_db)):
    lot = db.get(Lot, lot_id)
    if not lot:
        raise HTTPException(404, "Lot not found")
    return {"lot": S.lot(lot), "backward": trace.backward(db, lot), "forward": trace.forward(db, lot)}
