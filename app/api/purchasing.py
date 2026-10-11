"""Material availability, purchase requests (-> BC purchase orders) and reports."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..bc.sync import get_bc
from ..db import get_db
from ..events import broker
from ..models import PurchaseRequest
from ..services import purchasing as P
from ..services.reports import performance_report
from . import serialize as S

router = APIRouter(prefix="/api")


class LineQty(BaseModel):
    line_id: int
    qty: float


class ReceiveIn(BaseModel):
    heat_numbers: dict[int, str] = {}  # line id -> heat no for lot-tracked items


def _pr(db: Session, pr_id: int) -> PurchaseRequest:
    pr = db.get(PurchaseRequest, pr_id)
    if not pr:
        raise HTTPException(404, "Purchase request not found")
    return pr


def _run(fn, *args):
    try:
        return fn(*args)
    except P.PurchasingError as e:
        raise HTTPException(409, str(e))
    except ValueError as e:
        raise HTTPException(502, f"Business Central error: {e}")


def _done(db: Session, pr: PurchaseRequest):
    db.commit()
    broker.publish("plan")
    return S.purchase_request(pr)


@router.get("/materials/availability")
def material_availability(db: Session = Depends(get_db)):
    return P.availability(db)


@router.get("/purchase-requests")
def list_requests(status: str | None = None, db: Session = Depends(get_db)):
    q = select(PurchaseRequest).order_by(PurchaseRequest.id.desc())
    if status:
        q = q.where(PurchaseRequest.status.in_(status.split(",")))
    return [S.purchase_request(pr) for pr in db.scalars(q)]


@router.post("/purchase-requests/from-shortages")
def from_shortages(operator: str = "", db: Session = Depends(get_db)):
    prs = P.create_from_shortages(db, operator)
    db.commit()
    broker.publish("plan")
    return [S.purchase_request(pr) for pr in prs]


@router.post("/purchase-requests/{pr_id}/line")
def update_line(pr_id: int, body: LineQty, db: Session = Depends(get_db)):
    pr = _pr(db, pr_id)
    _run(P.update_line, db, pr, body.line_id, body.qty)
    return _done(db, pr)


@router.post("/purchase-requests/{pr_id}/send")
def send(pr_id: int, db: Session = Depends(get_db)):
    pr = _pr(db, pr_id)
    _run(P.send_to_bc, db, pr, get_bc())
    return _done(db, pr)


@router.post("/purchase-requests/{pr_id}/receive")
def receive(pr_id: int, body: ReceiveIn | None = None, db: Session = Depends(get_db)):
    pr = _pr(db, pr_id)
    _run(P.receive, db, pr, get_bc(), (body.heat_numbers if body else {}))
    return _done(db, pr)


@router.post("/purchase-requests/{pr_id}/cancel")
def cancel(pr_id: int, db: Session = Depends(get_db)):
    pr = _pr(db, pr_id)
    _run(P.cancel, pr)
    return _done(db, pr)


@router.get("/reports/performance")
def report(weeks: int = 10, db: Session = Depends(get_db)):
    return performance_report(db, max(2, min(weeks, 52)))
