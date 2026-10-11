"""Domain model.

Items mirror Business Central (BC). Recipes are BOM + routing combined: the
components a product consumes and the stages (press, weld, powder coat...) it
passes through. Jobs are production orders; each job has one JobOperation per
stage that tracks how many units are queued, in process, done or scrapped
there, which is what drives the real-time shop-floor board.
"""
from datetime import date, datetime, timezone

from sqlalchemy import JSON, Boolean, Date, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint, event
from sqlalchemy.orm import Mapped, mapped_column, relationship

from . import clock
from .db import Base


def utcnow():
    return clock.now()


@event.listens_for(Base, "init", propagate=True)
def _stamp_created_at(target, args, kwargs):
    """Timestamp records when they're created, not when they're flushed."""
    if "created_at" in type(target).__dict__ and kwargs.get("created_at") is None:
        target.created_at = utcnow()


class Item(Base):
    __tablename__ = "items"
    id: Mapped[int] = mapped_column(primary_key=True)
    no: Mapped[str] = mapped_column(String(50), unique=True, index=True)
    description: Mapped[str] = mapped_column(String(200), default="")
    item_type: Mapped[str] = mapped_column(String(20), default="raw")  # raw | sub | finished
    uom: Mapped[str] = mapped_column(String(20), default="EA")
    # BC unit cost. Items use BC's Standard costing method, so this is the standard cost.
    unit_cost: Mapped[float] = mapped_column(Float, default=0.0)
    # Raw material whose lots / heat numbers must be recorded on receipt and consumption.
    # Made items (sub-assemblies, finished goods) always get an output lot per job.
    lot_tracked: Mapped[bool] = mapped_column(Boolean, default=False)
    # Purchasing defaults for raw material (BC's API v2.0 items don't expose a vendor).
    vendor_no: Mapped[str] = mapped_column(String(30), default="")
    vendor_name: Mapped[str] = mapped_column(String(100), default="")
    lead_time_days: Mapped[int] = mapped_column(Integer, default=7)
    qty_on_hand: Mapped[float] = mapped_column(Float, default=0.0)
    bc_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Stage(Base):
    __tablename__ = "stages"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(20), unique=True)
    name: Mapped[str] = mapped_column(String(100))
    sequence: Mapped[int] = mapped_column(Integer, default=10)
    rate_per_hour: Mapped[float] = mapped_column(Float, default=0.0)  # labour + overhead
    # Available hours per working day (shifts x machines/people), for OEE and loading.
    capacity_hours_per_day: Mapped[float] = mapped_column(Float, default=7.6)
    color: Mapped[str] = mapped_column(String(20), default="#64748b")


class Recipe(Base):
    __tablename__ = "recipes"
    id: Mapped[int] = mapped_column(primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id"))
    version: Mapped[str] = mapped_column(String(20), default="1")
    output_qty: Mapped[float] = mapped_column(Float, default=1.0)
    # Typical run size that setup time is spread over in the standard cost
    # (None = one setup per output_qty).
    std_batch_qty: Mapped[float | None] = mapped_column(Float, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    notes: Mapped[str] = mapped_column(Text, default="")

    item: Mapped[Item] = relationship()
    operations: Mapped[list["RecipeOperation"]] = relationship(
        cascade="all, delete-orphan", order_by="RecipeOperation.sequence")
    components: Mapped[list["RecipeComponent"]] = relationship(cascade="all, delete-orphan")


class RecipeOperation(Base):
    __tablename__ = "recipe_operations"
    id: Mapped[int] = mapped_column(primary_key=True)
    recipe_id: Mapped[int] = mapped_column(ForeignKey("recipes.id", ondelete="CASCADE"))
    stage_id: Mapped[int] = mapped_column(ForeignKey("stages.id"))
    sequence: Mapped[int] = mapped_column(Integer)
    setup_minutes: Mapped[float] = mapped_column(Float, default=0.0)
    run_minutes_per_unit: Mapped[float] = mapped_column(Float, default=0.0)

    stage: Mapped[Stage] = relationship()


class RecipeComponent(Base):
    __tablename__ = "recipe_components"
    id: Mapped[int] = mapped_column(primary_key=True)
    recipe_id: Mapped[int] = mapped_column(ForeignKey("recipes.id", ondelete="CASCADE"))
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id"))
    qty_per: Mapped[float] = mapped_column(Float)  # per recipe.output_qty
    scrap_pct: Mapped[float] = mapped_column(Float, default=0.0)
    # Operation sequence at which this component is consumed; None = first operation.
    operation_seq: Mapped[int | None] = mapped_column(Integer, nullable=True)

    item: Mapped[Item] = relationship()


class Demand(Base):
    __tablename__ = "demands"
    id: Mapped[int] = mapped_column(primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id"))
    qty: Mapped[float] = mapped_column(Float)
    due_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    source: Mapped[str] = mapped_column(String(20), default="manual")  # manual | bc
    reference: Mapped[str] = mapped_column(String(100), default="")
    customer: Mapped[str] = mapped_column(String(100), default="")
    status: Mapped[str] = mapped_column(String(20), default="open")  # open | completed | cancelled
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    item: Mapped[Item] = relationship()


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[int] = mapped_column(primary_key=True)
    number: Mapped[str] = mapped_column(String(30), unique=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id"))
    recipe_id: Mapped[int] = mapped_column(ForeignKey("recipes.id"))
    demand_id: Mapped[int | None] = mapped_column(ForeignKey("demands.id"), nullable=True)
    qty: Mapped[float] = mapped_column(Float)
    # planned | released | in_progress | completed | posted | cancelled
    status: Mapped[str] = mapped_column(String(20), default="planned")
    due_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    qty_good: Mapped[float] = mapped_column(Float, default=0.0)
    qty_scrap: Mapped[float] = mapped_column(Float, default=0.0)
    material_cost: Mapped[float] = mapped_column(Float, default=0.0)
    conversion_cost: Mapped[float] = mapped_column(Float, default=0.0)
    unit_cost: Mapped[float] = mapped_column(Float, default=0.0)
    # Recipe standard cost per unit, frozen when the job is created.
    std_unit_cost: Mapped[float] = mapped_column(Float, default=0.0)
    # Full cost & variance breakdown, frozen when the job is closed.
    cost_snapshot: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    item: Mapped[Item] = relationship()
    recipe: Mapped[Recipe] = relationship()
    operations: Mapped[list["JobOperation"]] = relationship(
        cascade="all, delete-orphan", order_by="JobOperation.sequence")
    issues: Mapped[list["MaterialIssue"]] = relationship(cascade="all, delete-orphan")


class JobOperation(Base):
    __tablename__ = "job_operations"
    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"))
    stage_id: Mapped[int] = mapped_column(ForeignKey("stages.id"))
    sequence: Mapped[int] = mapped_column(Integer)
    setup_minutes: Mapped[float] = mapped_column(Float, default=0.0)
    run_minutes_per_unit: Mapped[float] = mapped_column(Float, default=0.0)
    qty_queued: Mapped[float] = mapped_column(Float, default=0.0)
    qty_in_process: Mapped[float] = mapped_column(Float, default=0.0)
    qty_done: Mapped[float] = mapped_column(Float, default=0.0)
    qty_scrap: Mapped[float] = mapped_column(Float, default=0.0)
    actual_minutes: Mapped[float] = mapped_column(Float, default=0.0)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    job: Mapped[Job] = relationship(back_populates="operations")
    stage: Mapped[Stage] = relationship()


class MaterialIssue(Base):
    __tablename__ = "material_issues"
    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"))
    job_operation_id: Mapped[int | None] = mapped_column(ForeignKey("job_operations.id"), nullable=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id"))
    lot_id: Mapped[int | None] = mapped_column(ForeignKey("lots.id"), nullable=True)
    qty: Mapped[float] = mapped_column(Float)
    unit_cost: Mapped[float] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    item: Mapped[Item] = relationship()
    lot: Mapped["Lot | None"] = relationship()


class Lot(Base):
    """A lot of an item. Raw material lots are received with their heat number,
    supplier and mill cert; made items get one output lot per job (lot no = job
    number). Material issues record which lot was consumed, which gives full
    genealogy: finished lot -> job -> consumed lots -> their jobs -> ... -> heat."""
    __tablename__ = "lots"
    __table_args__ = (UniqueConstraint("item_id", "lot_no"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id"))
    lot_no: Mapped[str] = mapped_column(String(50), index=True)
    heat_no: Mapped[str] = mapped_column(String(50), default="", index=True)
    supplier: Mapped[str] = mapped_column(String(100), default="")
    cert_ref: Mapped[str] = mapped_column(String(100), default="")
    source: Mapped[str] = mapped_column(String(20), default="receipt")  # receipt | output
    job_id: Mapped[int | None] = mapped_column(ForeignKey("jobs.id"), nullable=True)
    qty_received: Mapped[float] = mapped_column(Float, default=0.0)
    qty_on_hand: Mapped[float] = mapped_column(Float, default=0.0)
    notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    item: Mapped[Item] = relationship()


class StageEvent(Base):
    """Append-only shop-floor log: every start / complete / scrap / issue."""
    __tablename__ = "stage_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"))
    job_operation_id: Mapped[int | None] = mapped_column(ForeignKey("job_operations.id"), nullable=True)
    stage_id: Mapped[int | None] = mapped_column(ForeignKey("stages.id"), nullable=True)
    action: Mapped[str] = mapped_column(String(20))
    qty: Mapped[float] = mapped_column(Float, default=0.0)
    minutes: Mapped[float] = mapped_column(Float, default=0.0)
    operator: Mapped[str] = mapped_column(String(100), default="")
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class BcPosting(Base):
    """Outbox of inventory movements to send to BC (consumption / output).

    Movements are recorded here first so the shop floor never waits on BC and
    nothing is lost if BC is unreachable; a processor sends pending rows.
    """
    __tablename__ = "bc_postings"
    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(20))  # consumption | output
    job_id: Mapped[int | None] = mapped_column(ForeignKey("jobs.id"), nullable=True)
    document_no: Mapped[str] = mapped_column(String(50))
    item_no: Mapped[str] = mapped_column(String(50))
    lot_no: Mapped[str] = mapped_column(String(50), default="")
    qty: Mapped[float] = mapped_column(Float)
    unit_cost: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending | posted | failed
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str] = mapped_column(Text, default="")
    response: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class IssueLog(Base):
    """Something that went wrong on the floor: a breakdown, a quality defect,
    a material problem. Downtime minutes feed OEE availability."""
    __tablename__ = "issue_logs"
    id: Mapped[int] = mapped_column(primary_key=True)
    number: Mapped[str] = mapped_column(String(20), unique=True)
    job_id: Mapped[int | None] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), nullable=True)
    job_operation_id: Mapped[int | None] = mapped_column(ForeignKey("job_operations.id"), nullable=True)
    stage_id: Mapped[int | None] = mapped_column(ForeignKey("stages.id"), nullable=True)
    # breakdown | changeover | material | quality | safety | waiting | other
    category: Mapped[str] = mapped_column(String(20), default="other")
    severity: Mapped[str] = mapped_column(String(10), default="medium")  # low | medium | high
    title: Mapped[str] = mapped_column(String(200))
    detail: Mapped[str] = mapped_column(Text, default="")
    minutes_lost: Mapped[float] = mapped_column(Float, default=0.0)
    qty_affected: Mapped[float] = mapped_column(Float, default=0.0)
    operator: Mapped[str] = mapped_column(String(100), default="")
    status: Mapped[str] = mapped_column(String(20), default="open")  # open | resolved
    resolution: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    job: Mapped["Job | None"] = relationship()
    stage: Mapped["Stage | None"] = relationship()


class JobNote(Base):
    """Operator note on a job (optionally at one stage): a short note plus optional detail."""
    __tablename__ = "job_notes"
    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"))
    job_operation_id: Mapped[int | None] = mapped_column(ForeignKey("job_operations.id"), nullable=True)
    stage_id: Mapped[int | None] = mapped_column(ForeignKey("stages.id"), nullable=True)
    note: Mapped[str] = mapped_column(String(300))
    detail: Mapped[str] = mapped_column(Text, default="")
    operator: Mapped[str] = mapped_column(String(100), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PurchaseRequest(Base):
    """Request to buy raw material for a shortfall. Sending it creates a
    purchase order in BC; receipt then happens in BC and flows back in when
    stock is pulled."""
    __tablename__ = "purchase_requests"
    id: Mapped[int] = mapped_column(primary_key=True)
    number: Mapped[str] = mapped_column(String(20), unique=True)
    vendor_no: Mapped[str] = mapped_column(String(30), default="")
    vendor_name: Mapped[str] = mapped_column(String(100), default="")
    # draft | sent (PO created in BC) | received | cancelled
    status: Mapped[str] = mapped_column(String(20), default="draft")
    bc_po_no: Mapped[str] = mapped_column(String(30), default="")
    needed_by: Mapped[date | None] = mapped_column(Date, nullable=True)
    notes: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[str] = mapped_column(String(100), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    lines: Mapped[list["PurchaseRequestLine"]] = relationship(cascade="all, delete-orphan")


class PurchaseRequestLine(Base):
    __tablename__ = "purchase_request_lines"
    id: Mapped[int] = mapped_column(primary_key=True)
    request_id: Mapped[int] = mapped_column(ForeignKey("purchase_requests.id", ondelete="CASCADE"))
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id"))
    qty: Mapped[float] = mapped_column(Float)
    unit_cost: Mapped[float] = mapped_column(Float, default=0.0)
    needed_by: Mapped[date | None] = mapped_column(Date, nullable=True)

    item: Mapped[Item] = relationship()
