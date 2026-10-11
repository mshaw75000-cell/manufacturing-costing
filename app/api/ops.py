"""Issue log and operator notes."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db import get_db
from ..events import broker
from ..models import IssueLog, Job, JobNote, Stage, utcnow
from . import serialize as S

router = APIRouter(prefix="/api")

CATEGORIES = ("breakdown", "changeover", "material", "quality", "safety", "waiting", "other")


class IssueIn(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    category: str = Field("other", pattern="^(" + "|".join(CATEGORIES) + ")$")
    severity: str = Field("medium", pattern="^(low|medium|high)$")
    detail: str = ""
    job_id: int | None = None
    operation_id: int | None = None
    stage_id: int | None = None
    minutes_lost: float = Field(0, ge=0)
    qty_affected: float = Field(0, ge=0)
    operator: str = ""


class ResolveIn(BaseModel):
    resolution: str = ""
    operator: str = ""


class NoteIn(BaseModel):
    note: str = Field(min_length=1, max_length=300)
    detail: str = ""
    operation_id: int | None = None
    operator: str = ""


def next_issue_number(db: Session) -> str:
    return f"ISS{(db.scalar(select(func.max(IssueLog.id))) or 0) + 1:05d}"


def log_issue(db: Session, body: IssueIn) -> IssueLog:
    stage_id = body.stage_id
    if body.job_id:
        job = db.get(Job, body.job_id)
        if not job:
            raise HTTPException(404, "Job not found")
        if body.operation_id:
            op = next((o for o in job.operations if o.id == body.operation_id), None)
            if not op:
                raise HTTPException(404, "Stage not found on this job")
            stage_id = op.stage_id
    issue = IssueLog(number=next_issue_number(db), job_id=body.job_id, job_operation_id=body.operation_id,
                     stage_id=stage_id, category=body.category, severity=body.severity, title=body.title,
                     detail=body.detail, minutes_lost=body.minutes_lost, qty_affected=body.qty_affected,
                     operator=body.operator)
    db.add(issue)
    db.flush()
    return issue


@router.get("/issues")
def list_issues(status: str | None = None, stage_id: int | None = None, job_id: int | None = None,
                category: str | None = None, limit: int = 300, db: Session = Depends(get_db)):
    q = select(IssueLog).order_by(IssueLog.created_at.desc(), IssueLog.id.desc()).limit(limit)
    if status:
        q = q.where(IssueLog.status == status)
    if stage_id:
        q = q.where(IssueLog.stage_id == stage_id)
    if job_id:
        q = q.where(IssueLog.job_id == job_id)
    if category:
        q = q.where(IssueLog.category == category)
    return [S.issue_log(i) for i in db.scalars(q)]


@router.post("/issues")
def create_issue(body: IssueIn, db: Session = Depends(get_db)):
    issue = log_issue(db, body)
    db.commit()
    broker.publish("shopfloor", {"issue": issue.number})
    return S.issue_log(issue)


@router.post("/issues/{issue_id}/resolve")
def resolve_issue(issue_id: int, body: ResolveIn, db: Session = Depends(get_db)):
    issue = db.get(IssueLog, issue_id)
    if not issue:
        raise HTTPException(404, "Issue not found")
    issue.status, issue.resolution, issue.resolved_at = "resolved", body.resolution, utcnow()
    db.commit()
    broker.publish("shopfloor", {"issue": issue.number})
    return S.issue_log(issue)


@router.get("/jobs/{job_id}/notes")
def list_notes(job_id: int, db: Session = Depends(get_db)):
    stages = {s.id: s.name for s in db.scalars(select(Stage))}
    return [S.note(n, stages) for n in db.scalars(select(JobNote).where(JobNote.job_id == job_id)
                                                   .order_by(JobNote.created_at.desc(), JobNote.id.desc()))]


@router.post("/jobs/{job_id}/notes")
def add_note(job_id: int, body: NoteIn, db: Session = Depends(get_db)):
    job = db.get(Job, job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    op = next((o for o in job.operations if o.id == body.operation_id), None) if body.operation_id else None
    if body.operation_id and not op:
        raise HTTPException(404, "Stage not found on this job")
    n = JobNote(job_id=job_id, job_operation_id=op.id if op else None, stage_id=op.stage_id if op else None,
                note=body.note, detail=body.detail, operator=body.operator)
    db.add(n)
    db.commit()
    broker.publish("shopfloor", {"job": job.number})
    return S.note(n, {s.id: s.name for s in db.scalars(select(Stage))})
