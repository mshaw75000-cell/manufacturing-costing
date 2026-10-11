"""Production jobs, shop-floor movements, live WIP board and the SSE stream."""
import asyncio
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..events import broker
from ..models import IssueLog, Item, Job, JobNote, Stage, StageEvent
from ..services import production as P
from ..services.costing import job_costs
from . import serialize as S

router = APIRouter(prefix="/api")


class JobIn(BaseModel):
    item_id: int
    qty: float = Field(gt=0)
    due_date: date | None = None


class MoveIn(BaseModel):
    operation_id: int
    qty: float = Field(gt=0)
    operator: str = ""
    minutes: float | None = Field(None, ge=0)
    force: bool = False
    from_state: str = "in_process"
    note: str = ""
    lot_choices: dict[int, int] | None = None  # component item id -> lot id


class IssueIn(BaseModel):
    item_id: int
    qty: float = Field(gt=0)
    operation_id: int | None = None
    operator: str = ""
    lot_id: int | None = None


def _job(db: Session, job_id: int) -> Job:
    job = db.get(Job, job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    return job


def _commit(db: Session, job: Job, detail=True):
    db.commit()
    broker.publish("shopfloor", {"job": job.number, "status": job.status})
    broker.publish("plan")
    return S.job(job, detail=detail)


def _run(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except P.InsufficientStock as e:
        raise HTTPException(409, {"message": str(e), "shortages": e.shortages})
    except P.ProductionError as e:
        raise HTTPException(409, str(e))


@router.get("/jobs")
def list_jobs(status: str | None = None, db: Session = Depends(get_db)):
    q = select(Job).order_by(Job.id.desc())
    if status:
        q = q.where(Job.status.in_(status.split(",")))
    return [S.job(j) for j in db.scalars(q)]


@router.post("/jobs")
def create_job(body: JobIn, db: Session = Depends(get_db)):
    job = _run(P.create_job, db, body.item_id, body.qty, body.due_date)
    return _commit(db, job)


@router.get("/jobs/{job_id}")
def get_job(job_id: int, db: Session = Depends(get_db)):
    job = _job(db, job_id)
    out = S.job(job, detail=True)
    stages = {s.id: s.name for s in db.scalars(select(Stage))}
    out["events"] = [{"action": e.action, "qty": e.qty, "minutes": e.minutes, "operator": e.operator,
                      "note": e.note, "stage": stages.get(e.stage_id), "created_at": e.created_at}
                     for e in db.scalars(select(StageEvent).where(StageEvent.job_id == job_id)
                                         .order_by(StageEvent.created_at.desc(), StageEvent.id.desc()))]
    out["notes"] = [S.note(n, stages) for n in db.scalars(
        select(JobNote).where(JobNote.job_id == job_id).order_by(JobNote.created_at.desc(), JobNote.id.desc()))]
    out["issue_logs"] = [S.issue_log(i) for i in db.scalars(
        select(IssueLog).where(IssueLog.job_id == job_id).order_by(IssueLog.created_at.desc(), IssueLog.id.desc()))]
    return out


@router.post("/jobs/{job_id}/release")
def release(job_id: int, operator: str = "", db: Session = Depends(get_db)):
    job = _job(db, job_id)
    _run(P.release_job, db, job, operator)
    return _commit(db, job)


@router.post("/jobs/{job_id}/start")
def start(job_id: int, body: MoveIn, db: Session = Depends(get_db)):
    job = _job(db, job_id)
    _run(P.start, db, job, body.operation_id, body.qty, body.operator, body.force, body.lot_choices)
    return _commit(db, job)


@router.post("/jobs/{job_id}/complete")
def complete(job_id: int, body: MoveIn, db: Session = Depends(get_db)):
    job = _job(db, job_id)
    _run(P.complete, db, job, body.operation_id, body.qty, body.minutes, body.operator)
    return _commit(db, job)


@router.post("/jobs/{job_id}/scrap")
def scrap(job_id: int, body: MoveIn, db: Session = Depends(get_db)):
    job = _job(db, job_id)
    _run(P.scrap, db, job, body.operation_id, body.qty, body.from_state, body.operator, body.note)
    return _commit(db, job)


@router.post("/jobs/{job_id}/issue")
def issue(job_id: int, body: IssueIn, db: Session = Depends(get_db)):
    """Manually issue extra material to a job (e.g. rework, unplanned use)."""
    job = _job(db, job_id)
    if job.status not in ("released", "in_progress"):
        raise HTTPException(409, f"Job is {job.status}")
    item = db.get(Item, body.item_id)
    if not item:
        raise HTTPException(404, "Item not found")
    op = _run(P._op, job, body.operation_id) if body.operation_id else None
    _run(P.issue_material, db, job, item, body.qty, op, body.operator, body.lot_id)
    return _commit(db, job)


@router.post("/jobs/{job_id}/finish")
def finish(job_id: int, operator: str = "", db: Session = Depends(get_db)):
    job = _job(db, job_id)
    _run(P.finish, db, job, operator)
    return _commit(db, job)


@router.post("/jobs/{job_id}/cancel")
def cancel(job_id: int, db: Session = Depends(get_db)):
    job = _job(db, job_id)
    _run(P.cancel, db, job)
    return _commit(db, job)


@router.get("/wip")
def wip(db: Session = Depends(get_db)):
    """Where everything is right now: per stage, the jobs and units waiting
    there, being worked on, and the material that has been issued into it."""
    stages = list(db.scalars(select(Stage).order_by(Stage.sequence)))
    board = {s.id: {**S.stage(s), "queued": 0.0, "in_process": 0.0, "jobs": []} for s in stages}
    jobs = db.scalars(select(Job).where(Job.status.in_(("released", "in_progress")))).all()
    open_issues: dict[int, int] = {}
    for i in db.scalars(select(IssueLog).where(IssueLog.status == "open", IssueLog.job_operation_id.is_not(None))):
        open_issues[i.job_operation_id] = open_issues.get(i.job_operation_id, 0) + 1
    note_count: dict[int, int] = {}
    for n in db.scalars(select(JobNote).where(JobNote.job_id.in_([j.id for j in jobs]))):
        note_count[n.job_id] = note_count.get(n.job_id, 0) + 1
    for job in jobs:
        issued_by_op: dict[int, list] = {}
        for i in job.issues:
            issued_by_op.setdefault(i.job_operation_id, []).append(i)
        for op in job.operations:
            if op.qty_queued <= P.EPS and op.qty_in_process <= P.EPS:
                continue
            col = board[op.stage_id]
            col["queued"] += op.qty_queued
            col["in_process"] += op.qty_in_process
            materials = {}
            for i in issued_by_op.get(op.id, []):
                lot = i.lot
                m = materials.setdefault((i.item.no, i.lot_id), {
                    "item_no": i.item.no, "description": i.item.description, "qty": 0.0, "uom": i.item.uom,
                    "lot_no": lot.lot_no if lot else "", "heat_no": lot.heat_no if lot else ""})
                m["qty"] += i.qty
            col["jobs"].append({
                "job_id": job.id, "number": job.number, "item_no": job.item.no,
                "description": job.item.description, "job_qty": job.qty, "due_date": job.due_date,
                "operation_id": op.id, "sequence": op.sequence, "is_last": op is job.operations[-1],
                "qty_queued": op.qty_queued, "qty_in_process": op.qty_in_process,
                "qty_done": op.qty_done, "qty_scrap": op.qty_scrap,
                "materials": list(materials.values()),
                "open_issues": open_issues.get(op.id, 0), "notes": note_count.get(job.id, 0),
            })
    # Every unit is through (done or scrapped): ready to close and freeze costs.
    finished = [{"job_id": j.id, "number": j.number, "item_no": j.item.no, "qty_good": j.qty_good,
                 "qty_scrap": j.qty_scrap}
                for j in jobs if j.status == "in_progress" and P.units_remaining(j) <= P.EPS]
    return {"stages": list(board.values()), "awaiting_finish": finished}


@router.get("/wip/value")
def wip_value(db: Session = Depends(get_db)):
    """Value currently tied up in open jobs (material issued + conversion so far)."""
    rows = []
    for job in db.scalars(select(Job).where(Job.status.in_(("released", "in_progress")))):
        c = job_costs(job)
        # WIP = cost put in so far minus what has already gone to stock at standard
        rows.append({"number": job.number, "item_no": job.item.no, "total_cost": c["total_cost"],
                     "output_value": c["bc_value"], "wip": round(c["total_cost"] - c["bc_value"], 4)})
    return {"jobs": rows, "total": round(sum(r["wip"] for r in rows), 2)}


@router.get("/events")
def recent_events(limit: int = 50, db: Session = Depends(get_db)):
    stages = {s.id: s.name for s in db.scalars(select(Stage))}
    jobs = {}
    out = []
    for e in db.scalars(select(StageEvent).order_by(StageEvent.created_at.desc(), StageEvent.id.desc()).limit(limit)):
        if e.job_id not in jobs:
            jobs[e.job_id] = db.get(Job, e.job_id)
        j = jobs[e.job_id]
        out.append({"job": j.number, "item_no": j.item.no, "action": e.action, "qty": e.qty,
                    "stage": stages.get(e.stage_id), "operator": e.operator, "note": e.note,
                    "created_at": e.created_at})
    return out


@router.get("/stream")
async def stream(request: Request):
    q = broker.subscribe()

    async def gen():
        try:
            yield "retry: 3000\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=15)
                    yield f"data: {msg}\n\n"
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
        finally:
            broker.unsubscribe(q)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

