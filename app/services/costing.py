"""Standard costing.

Items are on BC's Standard costing method, so BC values every movement at the
item's standard cost (its BC unit cost):

  * material is issued to jobs at the component's standard cost;
  * finished units go into BC inventory at the output item's standard cost.

Actual job cost = material issued + actual stage time x stage hourly rate.
The gap between actual cost and what BC books for the output is the
production variance, broken down as:

  material usage  (actual qty issued - standard qty for good output) x std cost
  conversion      actual stage cost - standard stage cost for good output
  standard gap    recipe standard - BC standard of the output item
                  (non-zero means the BC standard is out of date vs the recipe)

so that   actual - BC value = material usage + conversion + standard gap.
"""
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Job, Recipe
from .planning import component_qty


def active_recipes(db: Session) -> dict[int, Recipe]:
    return {r.item_id: r for r in db.scalars(select(Recipe).where(Recipe.active.is_(True)))}


def component_std_cost(item, active: dict[int, Recipe], _seen=()) -> float:
    """BC standard cost of an item; for a made item without one yet, roll it up from its recipe."""
    if item.unit_cost > 0:
        return item.unit_cost
    sub = active.get(item.id)
    if sub and sub.id not in _seen:
        return standard_unit_cost(sub, active, _seen)
    return 0.0


def standard_unit_cost(r: Recipe, active: dict[int, Recipe], _seen=()) -> float:
    """Recipe standard cost per output unit: components at standard plus stage
    time at standard minutes, with setup spread over the standard batch size."""
    if not r.output_qty:
        return 0.0
    seen = _seen + (r.id,)
    material = sum(c.qty_per / r.output_qty * (1 + c.scrap_pct / 100) * component_std_cost(c.item, active, seen)
                   for c in r.components)
    batch = r.std_batch_qty or r.output_qty
    conversion = sum((o.setup_minutes / batch + o.run_minutes_per_unit) / 60 * o.stage.rate_per_hour
                     for o in r.operations)
    return material + conversion


def output_unit_cost(job: Job) -> float:
    """Value per unit at which finished output goes into BC: the item's BC
    standard cost, or the recipe standard if BC has none yet."""
    return job.item.unit_cost if job.item.unit_cost > 0 else job.std_unit_cost


def job_costs(job: Job) -> dict:
    good = job.qty_good
    recipe = job.recipe

    issued: dict[int, dict] = {}
    for i in job.issues:
        row = issued.setdefault(i.item_id, {"item_no": i.item.no, "qty": 0.0, "value": 0.0})
        row["qty"] += i.qty
        row["value"] += i.qty * i.unit_cost

    materials = []
    for comp in recipe.components:
        act = issued.pop(comp.item_id, {"qty": 0.0, "value": 0.0})
        std_cost = comp.item.unit_cost
        std_qty = component_qty(recipe, comp, good)
        materials.append({"item_no": comp.item.no, "uom": comp.item.uom, "std_qty": std_qty,
                          "actual_qty": act["qty"], "std_cost": std_cost,
                          "actual_value": act["value"], "std_value": std_qty * std_cost,
                          "variance": act["value"] - std_qty * std_cost})
    for item_id, act in issued.items():  # unplanned material, not in the recipe
        materials.append({"item_no": act["item_no"], "uom": "", "std_qty": 0.0, "actual_qty": act["qty"],
                          "std_cost": 0.0, "actual_value": act["value"], "std_value": 0.0,
                          "variance": act["value"]})

    stages = []
    for op in job.operations:
        rate = op.stage.rate_per_hour
        std_min = (op.setup_minutes + op.run_minutes_per_unit * good) if good > 0 else 0.0
        stages.append({"stage": op.stage.name, "rate": rate, "std_minutes": std_min,
                       "actual_minutes": op.actual_minutes, "std_value": std_min / 60 * rate,
                       "actual_value": op.actual_minutes / 60 * rate,
                       "variance": (op.actual_minutes - std_min) / 60 * rate})

    act_mat = sum(m["actual_value"] for m in materials)
    act_conv = sum(s["actual_value"] for s in stages)
    std_mat = sum(m["std_value"] for m in materials)
    std_conv = sum(s["std_value"] for s in stages)
    actual_total = act_mat + act_conv
    out_cost = output_unit_cost(job)
    bc_value = good * out_cost
    r4 = lambda x: round(x, 4)  # noqa: E731
    return {
        "material_cost": r4(act_mat), "conversion_cost": r4(act_conv), "total_cost": r4(actual_total),
        "unit_cost": r4(actual_total / good) if good > 0 else 0.0,
        "std_unit_cost": r4(job.std_unit_cost), "output_unit_cost": r4(out_cost),
        "bc_value": r4(bc_value),
        "variance": {
            "material_usage": r4(act_mat - std_mat),
            "conversion": r4(act_conv - std_conv),
            "standard_gap": r4(std_mat + std_conv - bc_value),
            "total": r4(actual_total - bc_value),
        },
        "materials": [{k: (r4(v) if isinstance(v, float) else v) for k, v in m.items()} for m in materials],
        "stages": [{k: (r4(v) if isinstance(v, float) else v) for k, v in s.items()} for s in stages],
    }
