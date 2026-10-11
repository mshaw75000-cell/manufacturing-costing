"""Demand (from BC or manual) and the material requirements plan."""
from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..events import broker
from ..models import Demand, Item
from ..services import production as P
from ..services.planning import RecipeCycleError, build_plan
from . import serialize as S

router = APIRouter(prefix="/api")


class DemandIn(BaseModel):
    item_id: int
    qty: float = Field(gt=0)
    due_date: date | None = None
    reference: str = ""


@router.get("/demand")
def list_demand(status: str | None = "open", db: Session = Depends(get_db)):
    q = select(Demand).order_by(Demand.due_date.is_(None), Demand.due_date, Demand.id)
    if status:
        q = q.where(Demand.status == status)
    return [S.demand(d) for d in db.scalars(q)]


@router.post("/demand")
def create_demand(body: DemandIn, db: Session = Depends(get_db)):
    if not db.get(Item, body.item_id):
        raise HTTPException(404, "Item not found")
    d = Demand(**body.model_dump(), source="manual")
    db.add(d)
    db.commit()
    broker.publish("plan")
    return S.demand(d)


@router.post("/demand/{demand_id}/cancel")
def cancel_demand(demand_id: int, db: Session = Depends(get_db)):
    d = db.get(Demand, demand_id)
    if not d:
        raise HTTPException(404, "Demand not found")
    d.status = "cancelled"
    db.commit()
    broker.publish("plan")
    return S.demand(d)


@router.post("/demand/{demand_id}/create-job")
def job_from_demand(demand_id: int, db: Session = Depends(get_db)):
    d = db.get(Demand, demand_id)
    if not d:
        raise HTTPException(404, "Demand not found")
    try:
        job = P.create_job(db, d.item_id, d.qty, d.due_date, demand_id=d.id)
    except P.ProductionError as e:
        raise HTTPException(409, str(e))
    db.commit()
    broker.publish("shopfloor")
    return S.job(job)


@router.get("/plan")
def get_plan(db: Session = Depends(get_db)):
    try:
        return build_plan(db)
    except RecipeCycleError as e:
        raise HTTPException(422, str(e))


@router.post("/plan/create-jobs")
def create_planned_jobs(db: Session = Depends(get_db)):
    """Create planned jobs for every 'make' line in the current plan."""
    plan = build_plan(db)
    jobs = []
    for line in plan["lines"]:
        if line["action"] == "make":
            jobs.append(P.create_job(db, line["item_id"], line["net"], line["earliest_due"]))
    db.commit()
    broker.publish("shopfloor")
    return [S.job(j) for j in jobs]
