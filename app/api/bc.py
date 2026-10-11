"""Business Central sync: pull items/inventory/costs and demand, push postings."""
import os

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..bc.sync import get_bc, process_outbox, sync_demand, sync_items
from ..db import get_db
from ..events import broker
from ..models import BcPosting

router = APIRouter(prefix="/api/bc")


def _guard(fn, *args):
    try:
        return fn(*args)
    except KeyError as e:
        raise HTTPException(500, f"Missing BC setting {e}")
    except Exception as e:
        raise HTTPException(502, f"Business Central error: {e}")


@router.get("/status")
def status(db: Session = Depends(get_db)):
    counts = dict(db.execute(select(BcPosting.status, func.count()).group_by(BcPosting.status)).all())
    return {"mode": os.environ.get("BC_MODE", "mock"), "postings": counts}


@router.post("/sync-items")
def bc_sync_items(db: Session = Depends(get_db)):
    result = _guard(sync_items, db, get_bc())
    db.commit()
    broker.publish("plan")
    return result


@router.post("/sync-demand")
def bc_sync_demand(db: Session = Depends(get_db)):
    result = _guard(sync_demand, db, get_bc())
    db.commit()
    broker.publish("plan")
    return result


@router.post("/process-postings")
def bc_process_postings(db: Session = Depends(get_db)):
    result = _guard(process_outbox, db, get_bc())
    broker.publish("bc")
    return result


@router.get("/postings")
def list_postings(status: str | None = None, limit: int = 200, db: Session = Depends(get_db)):
    q = select(BcPosting).order_by(BcPosting.id.desc()).limit(limit)
    if status:
        q = q.where(BcPosting.status == status)
    return [{"id": p.id, "kind": p.kind, "document_no": p.document_no, "item_no": p.item_no, "lot_no": p.lot_no,
             "qty": p.qty, "unit_cost": p.unit_cost, "status": p.status, "attempts": p.attempts,
             "error": p.error, "created_at": p.created_at, "posted_at": p.posted_at}
            for p in db.scalars(q)]
