"""Domain model.

Items mirror Business Central (BC). Recipes are BOM + routing combined: the
components a product consumes and the stages (press, weld, powder coat...) it
passes through. Jobs are production orders; each job has one JobOperation per
stage that tracks how many units are queued, in process, done or scrapped
there, which is what drives the real-time shop-floor board.
"""
from datetime import date, datetime, timezone

from sqlalchemy import JSON, Boolean, Date, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def utcnow():
    return datetime.now(timezone.utc)


class Item(Base):
    __tablename__ = "items"
    id: Mapped[int] = mapped_column(primary_key=True)
    no: Mapped[str] = mapped_column(String(50), unique=True, index=True)
    description: Mapped[str] = mapped_column(String(200), default="")
    item_type: Mapped[str] = mapped_column(String(20), default="raw")  # raw | sub | finished
    uom: Mapped[str] = mapped_column(String(20), default="EA")
    unit_cost: Mapped[float] = mapped_column(Float, default=0.0)
    qty_on_hand: Mapped[float] = mapped_column(Float, default=0.0)
    bc_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Stage(Base):
    __tablename__ = "stages"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(20), unique=True)
    name: Mapped[str] = mapped_column(String(100))
    sequence: Mapped[int] = mapped_column(Integer, default=10)
    rate_per_hour: Mapped[float] = mapped_column(Float, default=0.0)  # labour + overhead
    color: Mapped[str] = mapped_column(String(20), default="#64748b")


class Recipe(Base):
    __tablename__ = "recipes"
    id: Mapped[int] = mapped_column(primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id"))
    version: Mapped[str] = mapped_column(String(20), default="1")
    output_qty: Mapped[float] = mapped_column(Float, default=1.0)
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
    qty: Mapped[float] = mapped_column(Float)
    unit_cost: Mapped[float] = mapped_column(Float)
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
    qty: Mapped[float] = mapped_column(Float)
    unit_cost: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending | posted | failed
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str] = mapped_column(Text, default="")
    response: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
