"""Production jobs: release, stage movements, material consumption and costing.

Flow of units through a job's operations (one per stage):

    queued --start--> in process --complete--> done  (and queued at next stage)
       \\_____________ scrap _______/

Components are backflushed when units *start* at the operation where the
recipe consumes them, so the material shows up at that stage straight away,
and the local inventory is reduced with a consumption posting queued for BC.
Consumption is drawn from the item's lots (the operator's chosen lot first,
then oldest first), so every issue records the lot / heat it came from.

Finished units are posted to BC as they complete the last stage, at the
output item's standard cost, into the job's output lot (lot no = job number).
Closing the job freezes actual cost and variances (see costing.py).
"""
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import BcPosting, Demand, Job, JobOperation, Lot, MaterialIssue, StageEvent, utcnow
from .costing import active_recipes, job_costs, output_unit_cost, standard_unit_cost
from .planning import active_recipe, component_qty

EPS = 1e-9


class ProductionError(ValueError):
    pass


class InsufficientStock(ProductionError):
    def __init__(self, shortages: list[dict]):
        self.shortages = shortages
        names = ", ".join(f"{s['item_no']} (need {s['need']:g}, have {s['on_hand']:g})" for s in shortages)
        super().__init__(f"Insufficient stock: {names}")


def _next_job_number(db: Session) -> str:
    n = (db.scalar(select(func.max(Job.id))) or 0) + 1
    return f"MO{n:06d}"


def create_job(db: Session, item_id: int, qty: float, due_date: date | None = None,
               demand_id: int | None = None) -> Job:
    if qty <= 0:
        raise ProductionError("Quantity must be positive")
    recipe = active_recipe(db, item_id)
    if not recipe:
        raise ProductionError("No active recipe for this item")
    if not recipe.operations:
        raise ProductionError("Recipe has no operations (stages)")
    job = Job(number=_next_job_number(db), item_id=item_id, recipe_id=recipe.id, qty=qty,
              due_date=due_date, demand_id=demand_id,
              std_unit_cost=round(standard_unit_cost(recipe, active_recipes(db)), 4))
    for op in recipe.operations:
        job.operations.append(JobOperation(
            stage_id=op.stage_id, sequence=op.sequence,
            setup_minutes=op.setup_minutes, run_minutes_per_unit=op.run_minutes_per_unit))
    db.add(job)
    db.flush()
    _log(db, job, None, "create", qty)
    return job


def release_job(db: Session, job: Job, operator: str = "") -> Job:
    if job.status != "planned":
        raise ProductionError(f"Job is {job.status}, only planned jobs can be released")
    job.status = "released"
    job.operations[0].qty_queued = job.qty
    _log(db, job, job.operations[0], "release", job.qty, operator=operator)
    return job


def _op(job: Job, op_id: int) -> JobOperation:
    for op in job.operations:
        if op.id == op_id:
            return op
    raise ProductionError("Operation not found on this job")


def _components_for_op(job: Job, op: JobOperation):
    first_seq = job.operations[0].sequence
    for comp in job.recipe.components:
        seq = comp.operation_seq if comp.operation_seq is not None else first_seq
        if seq == op.sequence:
            yield comp


def start(db: Session, job: Job, op_id: int, qty: float, operator: str = "", force: bool = False,
          lot_choices: dict[int, int] | None = None):
    """Start units at a stage. lot_choices maps component item id -> preferred lot id."""
    if job.status not in ("released", "in_progress"):
        raise ProductionError(f"Job is {job.status}")
    op = _op(job, op_id)
    if qty <= 0 or qty > op.qty_queued + EPS:
        raise ProductionError(f"Can start between 0 and {op.qty_queued:g} at this stage")

    needs = [(c, component_qty(job.recipe, c, qty)) for c in _components_for_op(job, op)]
    shortages = [
        {"item_no": c.item.no, "need": q, "on_hand": c.item.qty_on_hand}
        for c, q in needs if c.item.qty_on_hand + EPS < q
    ]
    if shortages and not force:
        raise InsufficientStock(shortages)

    op.qty_queued -= qty
    op.qty_in_process += qty
    setup = 0.0
    if op.started_at is None:
        op.started_at = utcnow()
        setup = op.setup_minutes
        op.actual_minutes += setup
    job.status = "in_progress"
    for comp, q in needs:
        issue_material(db, job, comp.item, q, op, operator, (lot_choices or {}).get(comp.item_id))
    # minutes on a start event = setup time, so reports can bucket all stage time by date
    _log(db, job, op, "start", qty, minutes=setup, operator=operator)


def allocate_lots(db: Session, item, qty: float, preferred_lot_id: int | None = None):
    """Split qty across the item's lots: preferred lot first, then oldest first.
    Returns [(lot or None, qty)]; None covers stock that isn't in any lot."""
    lots = list(db.scalars(select(Lot).where(Lot.item_id == item.id, Lot.qty_on_hand > EPS)
                           .order_by(Lot.created_at, Lot.id)))
    if preferred_lot_id:
        pref = [l for l in lots if l.id == preferred_lot_id]
        if not pref:
            raise ProductionError(f"Lot {preferred_lot_id} has no stock of {item.no}")
        lots = pref + [l for l in lots if l.id != preferred_lot_id]
    parts, remaining = [], qty
    for lot in lots:
        if remaining <= EPS:
            break
        take = min(lot.qty_on_hand, remaining)
        parts.append((lot, take))
        remaining -= take
    if remaining > EPS:
        parts.append((None, remaining))
    return parts


def issue_material(db: Session, job: Job, item, qty: float, op: JobOperation | None = None,
                   operator: str = "", preferred_lot_id: int | None = None):
    item.qty_on_hand -= qty
    for lot, q in allocate_lots(db, item, qty, preferred_lot_id):
        if lot:
            lot.qty_on_hand -= q
        job.issues.append(MaterialIssue(item_id=item.id, item=item, qty=q, unit_cost=item.unit_cost,
                                        lot_id=lot.id if lot else None, lot=lot,
                                        job_operation_id=op.id if op else None))
        db.add(BcPosting(kind="consumption", job_id=job.id, document_no=job.number, item_no=item.no,
                         lot_no=lot.lot_no if lot else "", qty=q, unit_cost=item.unit_cost))
        if lot:
            where = f"lot {lot.lot_no}" + (f", heat {lot.heat_no}" if lot.heat_no else "")
        else:
            where = "NO LOT" if item.lot_tracked else ""
        _log(db, job, op, "issue", q, operator=operator,
             note=f"{item.no} x {q:g} {item.uom}" + (f" ({where})" if where else ""))


def complete(db: Session, job: Job, op_id: int, qty: float, minutes: float | None = None,
             operator: str = ""):
    op = _op(job, op_id)
    if qty <= 0 or qty > op.qty_in_process + EPS:
        raise ProductionError(f"Can complete between 0 and {op.qty_in_process:g} at this stage")
    if minutes is None:
        minutes = op.run_minutes_per_unit * qty
    op.qty_in_process -= qty
    op.qty_done += qty
    op.actual_minutes += minutes
    idx = job.operations.index(op)
    if idx + 1 < len(job.operations):
        job.operations[idx + 1].qty_queued += qty
    else:
        _receive_output(db, job, qty)
    _close_op_if_done(op)
    _log(db, job, op, "complete", qty, minutes=minutes, operator=operator)


def _receive_output(db: Session, job: Job, qty: float):
    """Finished units go straight into stock and are posted to BC at standard cost."""
    job.qty_good += qty
    job.item.qty_on_hand += qty
    lot = db.scalar(select(Lot).where(Lot.item_id == job.item_id, Lot.lot_no == job.number))
    if not lot:
        lot = Lot(item_id=job.item_id, lot_no=job.number, source="output", job_id=job.id,
                  qty_received=0.0, qty_on_hand=0.0)
        db.add(lot)
    lot.qty_received += qty
    lot.qty_on_hand += qty
    cost = output_unit_cost(job)
    db.add(BcPosting(kind="output", job_id=job.id, document_no=job.number, item_no=job.item.no,
                     lot_no=job.number, qty=qty, unit_cost=cost))
    _log(db, job, None, "output", qty, note=f"{job.item.no} x {qty:g} to stock @ std {cost:.4f}")


def scrap(db: Session, job: Job, op_id: int, qty: float, from_state: str = "in_process",
          operator: str = "", note: str = ""):
    op = _op(job, op_id)
    attr = {"queued": "qty_queued", "in_process": "qty_in_process"}.get(from_state)
    if not attr:
        raise ProductionError("from_state must be 'queued' or 'in_process'")
    available = getattr(op, attr)
    if qty <= 0 or qty > available + EPS:
        raise ProductionError(f"Can scrap between 0 and {available:g}")
    setattr(op, attr, available - qty)
    op.qty_scrap += qty
    job.qty_scrap += qty
    _close_op_if_done(op)
    _log(db, job, op, "scrap", qty, operator=operator, note=note)


def _close_op_if_done(op: JobOperation):
    if op.qty_queued <= EPS and op.qty_in_process <= EPS and op.completed_at is None:
        op.completed_at = utcnow()


def units_remaining(job: Job) -> float:
    return sum(op.qty_queued + op.qty_in_process for op in job.operations)


def finish(db: Session, job: Job, operator: str = "") -> Job:
    """Close the job and freeze its actual cost and variances. Output has
    already been posted to BC as units completed. Units still waiting at a
    stage are written off as not produced (the job is closed short)."""
    if job.status not in ("released", "in_progress"):
        raise ProductionError(f"Job is {job.status}")
    if any(op.qty_in_process > EPS for op in job.operations):
        raise ProductionError("Units are still in process; complete or scrap them first")
    short = units_remaining(job)
    costs = job_costs(job)
    job.material_cost = costs["material_cost"]
    job.conversion_cost = costs["conversion_cost"]
    job.unit_cost = costs["unit_cost"]
    job.cost_snapshot = costs
    job.status = "completed"
    job.completed_at = utcnow()
    for op in job.operations:
        op.qty_queued = 0.0
    if job.demand_id:
        demand = db.get(Demand, job.demand_id)
        if demand and demand.status == "open":
            demand.status = "completed"
    note = f"Actual unit cost {job.unit_cost:.4f}, variance {costs['variance']['total']:.2f}"
    if short > EPS:
        note += f"; closed short, {short:g} units not started"
    _log(db, job, None, "finish", job.qty_good, operator=operator, note=note)
    return job


def cancel(db: Session, job: Job):
    if job.status != "planned":
        raise ProductionError("Only planned jobs can be cancelled")
    job.status = "cancelled"
    _log(db, job, None, "cancel", job.qty)


def _log(db: Session, job: Job, op: JobOperation | None, action: str, qty: float,
         minutes: float = 0.0, operator: str = "", note: str = ""):
    db.add(StageEvent(job_id=job.id, job_operation_id=op.id if op else None,
                      stage_id=op.stage_id if op else None, action=action, qty=qty,
                      minutes=minutes, operator=operator, note=note))
