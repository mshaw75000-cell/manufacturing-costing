"""Material requirements planning (MRP).

Explodes open demand through the recipes level by level (low-level codes), so a
sub-assembly used by several products is netted once against everything that
needs it. Netting per item:

    gross     = independent demand + dependent demand from parents
                + remaining component needs of open jobs (not yet issued)
    supply    = qty on hand + remaining output of open jobs + open purchase requests
    net       = max(0, gross - supply)

Net need of an item with an active recipe becomes planned production ("make"),
which in turn generates dependent demand for its components. Net need of an
item without a recipe is a raw-material shortfall ("buy").
"""
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Demand, Item, Job, PurchaseRequest, Recipe

OPEN_JOB_STATUSES = ("planned", "released", "in_progress")
OPEN_PR_STATUSES = ("draft", "sent")


class RecipeCycleError(ValueError):
    pass


def active_recipe(db: Session, item_id: int) -> Recipe | None:
    return db.scalars(
        select(Recipe).where(Recipe.item_id == item_id, Recipe.active.is_(True)).order_by(Recipe.id.desc())
    ).first()


def component_qty(recipe: Recipe, comp, output_qty: float) -> float:
    return output_qty / recipe.output_qty * comp.qty_per * (1 + comp.scrap_pct / 100.0)


def job_remaining_components(job: Job) -> dict[int, float]:
    """Component quantities a job still needs that have not been issued yet."""
    issued = defaultdict(float)
    for iss in job.issues:
        issued[iss.item_id] += iss.qty
    remaining = {}
    for comp in job.recipe.components:
        need = component_qty(job.recipe, comp, job.qty) - issued[comp.item_id]
        if need > 1e-9:
            remaining[comp.item_id] = remaining.get(comp.item_id, 0.0) + need
    return remaining


def job_remaining_output(job: Job) -> float:
    return max(0.0, job.qty - job.qty_good - job.qty_scrap)


@dataclass
class PlanLine:
    item_id: int
    item_no: str
    description: str
    item_type: str
    uom: str
    level: int
    gross: float = 0.0
    on_hand: float = 0.0
    scheduled_receipts: float = 0.0
    net: float = 0.0
    action: str = "ok"  # ok | make | buy
    unit_cost: float = 0.0
    shortfall_value: float = 0.0
    earliest_due: date | None = None
    sources: list[str] = field(default_factory=list)


def _low_level_codes(recipes: dict[int, Recipe]) -> dict[int, int]:
    codes: dict[int, int] = defaultdict(int)

    def walk(item_id: int, level: int, path: tuple[int, ...]):
        if item_id in path:
            raise RecipeCycleError(f"Recipe cycle involving item id {item_id}")
        codes[item_id] = max(codes[item_id], level)
        recipe = recipes.get(item_id)
        if recipe:
            for comp in recipe.components:
                walk(comp.item_id, level + 1, path + (item_id,))

    for item_id in list(recipes):
        walk(item_id, 0, ())
    return codes


def build_plan(db: Session) -> dict:
    items = {i.id: i for i in db.scalars(select(Item))}
    recipes = {}
    for item_id in items:
        r = active_recipe(db, item_id)
        if r:
            recipes[item_id] = r

    gross = defaultdict(float)
    receipts = defaultdict(float)
    due: dict[int, date | None] = {}
    sources = defaultdict(list)

    def note_due(item_id, d):
        if d and (due.get(item_id) is None or d < due[item_id]):
            due[item_id] = d

    for d in db.scalars(select(Demand).where(Demand.status == "open")):
        gross[d.item_id] += d.qty
        note_due(d.item_id, d.due_date)
        sources[d.item_id].append(f"{d.source.upper()} {d.reference or '#' + str(d.id)}: {d.qty:g}")

    for pr in db.scalars(select(PurchaseRequest).where(PurchaseRequest.status.in_(OPEN_PR_STATUSES))):
        for ln in pr.lines:
            receipts[ln.item_id] += ln.qty
            sources[ln.item_id].append(f"On order {pr.bc_po_no or pr.number}: {ln.qty:g}")

    for job in db.scalars(select(Job).where(Job.status.in_(OPEN_JOB_STATUSES))):
        receipts[job.item_id] += job_remaining_output(job)
        for item_id, qty in job_remaining_components(job).items():
            gross[item_id] += qty
            note_due(item_id, job.due_date)
            sources[item_id].append(f"Job {job.number}: {qty:g}")

    codes = _low_level_codes(recipes)
    for item_id in gross:
        codes.setdefault(item_id, 0)
    max_level = max(codes.values(), default=0)

    lines: dict[int, PlanLine] = {}
    for level in range(max_level + 1):
        for item_id in [i for i, lv in codes.items() if lv == level]:
            item = items[item_id]
            if gross[item_id] <= 1e-9 and item_id not in receipts:
                continue
            line = PlanLine(
                item_id=item_id, item_no=item.no, description=item.description,
                item_type=item.item_type, uom=item.uom, level=level,
                gross=gross[item_id], on_hand=item.qty_on_hand,
                scheduled_receipts=receipts[item_id], unit_cost=item.unit_cost,
                earliest_due=due.get(item_id), sources=sources[item_id],
            )
            line.net = max(0.0, line.gross - max(0.0, line.on_hand) - line.scheduled_receipts)
            recipe = recipes.get(item_id)
            if line.net > 1e-9:
                if recipe:
                    line.action = "make"
                    # make whole batches of the recipe's output quantity
                    batches = -(-line.net // recipe.output_qty)
                    make_qty = batches * recipe.output_qty
                    line.net = make_qty
                    for comp in recipe.components:
                        q = component_qty(recipe, comp, make_qty)
                        gross[comp.item_id] += q
                        note_due(comp.item_id, line.earliest_due)
                        sources[comp.item_id].append(f"Planned {item.no}: {q:g}")
                else:
                    line.action = "buy"
                    line.shortfall_value = line.net * item.unit_cost
            lines[item_id] = line

    ordered = sorted(lines.values(), key=lambda l: (l.level, l.item_no))
    buy = [l for l in ordered if l.action == "buy"]
    return {
        "lines": [asdict(l) for l in ordered],
        "summary": {
            "make_lines": sum(1 for l in ordered if l.action == "make"),
            "buy_lines": len(buy),
            "shortfall_value": round(sum(l.shortfall_value for l in buy), 2),
        },
    }
