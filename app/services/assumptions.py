"""Hourly cost of each process / machine, built up from assumptions.

    labour        crew size x labour rate (wages + on-costs)
    electricity   machine kW x average load % x electricity price per kWh
    gas           gas cost per running hour (e.g. curing oven)
    consumables   blades, tips, filters... per hour
    maintenance   servicing and repairs per hour
    depreciation  machine cost / life in years / available hours per year
    overhead      share of rent, supervision, admin per hour

The total becomes the stage's rate per hour, which costs every minute of
stage time in actual job costs and recipe standard costs.
"""
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Recipe, Setting, Stage

DEFAULT_SETTINGS = {
    "electricity_per_kwh": 0.28,   # $ per kWh
    "working_days_per_week": 5,
    "working_weeks_per_year": 48,
}
SETTING_LABELS = {
    "electricity_per_kwh": "Electricity price ($ per kWh)",
    "working_days_per_week": "Working days per week",
    "working_weeks_per_year": "Working weeks per year",
}
ELEMENTS = ["labour", "electricity", "gas", "consumables", "maintenance", "depreciation", "overhead"]
ASSUMPTION_FIELDS = ["crew_size", "labour_rate", "machine_kw", "power_load_pct", "gas_per_hour",
                     "consumables_per_hour", "maintenance_per_hour", "machine_cost", "machine_life_years",
                     "overhead_per_hour", "capacity_hours_per_day", "assumption_notes"]


def get_settings(db: Session) -> dict[str, float]:
    out = dict(DEFAULT_SETTINGS)
    for s in db.scalars(select(Setting)):
        out[s.key] = s.value
    return out


def set_settings(db: Session, values: dict[str, float]):
    for k, v in values.items():
        if k not in DEFAULT_SETTINGS:
            raise ValueError(f"Unknown setting {k}")
        row = db.get(Setting, k) or Setting(key=k, value=v)
        row.value = float(v)
        db.add(row)
    db.flush()
    recompute_rates(db)


def annual_hours(stage: Stage, settings: dict) -> float:
    return stage.capacity_hours_per_day * settings["working_days_per_week"] * settings["working_weeks_per_year"]


def rate_breakdown(stage: Stage, settings: dict) -> dict[str, float]:
    v = lambda k: getattr(stage, k) or 0.0  # noqa: E731  (unsaved stages have no defaults yet)
    hours = (v("capacity_hours_per_day") * settings["working_days_per_week"] * settings["working_weeks_per_year"])
    dep = v("machine_cost") / v("machine_life_years") / hours if v("machine_cost") and v("machine_life_years") and hours else 0.0
    return {
        "labour": v("crew_size") * v("labour_rate"),
        "electricity": v("machine_kw") * v("power_load_pct") / 100 * settings["electricity_per_kwh"],
        "gas": v("gas_per_hour"),
        "consumables": v("consumables_per_hour"),
        "maintenance": v("maintenance_per_hour"),
        "depreciation": dep,
        "overhead": v("overhead_per_hour"),
    }


def stage_elements(stage: Stage, settings: dict) -> dict[str, float]:
    """Per-hour cost by element; a manually rated stage counts as all overhead."""
    if stage.rate_from_assumptions:
        return rate_breakdown(stage, settings)
    return {**{k: 0.0 for k in ELEMENTS}, "overhead": stage.rate_per_hour}


def recompute_rates(db: Session):
    settings = get_settings(db)
    for st in db.scalars(select(Stage)):
        if st.rate_from_assumptions:
            st.rate_per_hour = round(sum(rate_breakdown(st, settings).values()), 4)


def apply(db: Session, stage: Stage, values: dict):
    for k in ASSUMPTION_FIELDS:
        if k in values:
            setattr(stage, k, values[k] if k == "assumption_notes" else float(values[k] or 0))
    if any(k in values for k in ASSUMPTION_FIELDS if k not in ("capacity_hours_per_day", "assumption_notes")):
        stage.rate_from_assumptions = True
    if stage.rate_from_assumptions:
        stage.rate_per_hour = round(sum(rate_breakdown(stage, get_settings(db)).values()), 4)


def standard_breakdown(r: Recipe, active: dict, settings: dict) -> dict[str, float]:
    """Recipe standard cost per unit split into material and the hourly cost elements."""
    from .costing import component_std_cost
    out = {"material": 0.0, **{k: 0.0 for k in ELEMENTS}}
    if not r.output_qty:
        return out
    for c in r.components:
        out["material"] += c.qty_per / r.output_qty * (1 + c.scrap_pct / 100) * component_std_cost(c.item, active, (r.id,))
    batch = r.std_batch_qty or r.output_qty
    for o in r.operations:
        hours = (o.setup_minutes / batch + o.run_minutes_per_unit) / 60
        for k, v in stage_elements(o.stage, settings).items():
            out[k] += hours * v
    return {k: round(v, 4) for k, v in out.items()}


def summary(db: Session) -> dict:
    settings = get_settings(db)
    stages = []
    for st in db.scalars(select(Stage).order_by(Stage.sequence)):
        br = stage_elements(st, settings)
        hours = annual_hours(st, settings)
        stages.append({
            "id": st.id, "code": st.code, "name": st.name, "color": st.color, "sequence": st.sequence,
            "rate_from_assumptions": st.rate_from_assumptions, "rate_per_hour": st.rate_per_hour,
            **{k: getattr(st, k) for k in ASSUMPTION_FIELDS},
            "breakdown": {k: round(v, 4) for k, v in br.items()},
            "annual_hours": round(hours, 1), "annual_cost_at_capacity": round(st.rate_per_hour * hours, 0),
            "electricity_kwh_per_hour": round(st.machine_kw * st.power_load_pct / 100, 3),
        })
    return {"settings": settings, "labels": SETTING_LABELS, "stages": stages, "elements": ELEMENTS}
