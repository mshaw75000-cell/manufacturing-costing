"""Performance reporting: OEE, output vs capacity, average cost, opportunities.

All figures come from the shop-floor log (stage events), the issue log and
closed jobs, bucketed by week (Monday start).

Per stage and week, all in minutes:
  worked       actual stage time logged (setup on start + run time on complete)
  downtime     minutes lost recorded on issue logs
  loaded       worked + downtime  (time the stage was committed to jobs)
  earned       standard minutes for every unit processed (good + scrapped)
  earned_good  standard minutes for good units only

  Availability = worked / loaded
  Performance  = earned / worked        (capped at 100% inside OEE)
  Quality      = earned_good / earned
  OEE          = Availability x Performance x Quality

  Output vs capacity = earned_good / capacity, where capacity is the stage's
  available hours per day x 5 working days. It shows how much of what the
  stage could have produced was actually turned into good output.
"""
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import clock
from ..models import IssueLog, Job, JobOperation, Stage, StageEvent

WORKING_DAYS = 5


def week_start(d) -> date:
    if isinstance(d, datetime):
        d = d.date()
    return d - timedelta(days=d.weekday())


def _ratio(a, b):
    return a / b if b > 1e-9 else None


def _metrics(m: dict, capacity_min: float) -> dict:
    a = _ratio(m["worked"], m["worked"] + m["downtime"])
    p = _ratio(m["earned"], m["worked"])
    q = _ratio(m["earned_good"], m["earned"])
    oee = a * min(p, 1.0) * q if None not in (a, p, q) else None
    r = lambda x: round(x, 4) if x is not None else None  # noqa: E731
    return {"availability": r(a), "performance": r(p), "quality": r(q), "oee": r(oee),
            "output_vs_capacity": r(_ratio(m["earned_good"], capacity_min)),
            "worked_hours": round(m["worked"] / 60, 2), "downtime_hours": round(m["downtime"] / 60, 2),
            "earned_hours": round(m["earned_good"] / 60, 2), "capacity_hours": round(capacity_min / 60, 2),
            "good_units": m["good"], "scrap_units": m["scrap"]}


def _trend(points: list[tuple[float, float]]) -> dict | None:
    """Least-squares line through (x, y). Returns slope per x and fitted ends."""
    pts = [(x, y) for x, y in points if y is not None]
    if len(pts) < 2:
        return None
    n = len(pts)
    mx = sum(x for x, _ in pts) / n
    my = sum(y for _, y in pts) / n
    sxx = sum((x - mx) ** 2 for x, _ in pts)
    slope = sum((x - mx) * (y - my) for x, y in pts) / sxx if sxx else 0.0
    icpt = my - slope * mx
    return {"slope": round(slope, 5), "intercept": round(icpt, 5)}


def performance_report(db: Session, weeks: int = 10) -> dict:
    today = clock.now().date()
    first = week_start(today) - timedelta(weeks=weeks - 1)
    week_keys = [first + timedelta(weeks=i) for i in range(weeks)]
    since = datetime.combine(first, datetime.min.time(), tzinfo=timezone.utc)

    stages = list(db.scalars(select(Stage).order_by(Stage.sequence)))
    ops = {o.id: o for o in db.scalars(select(JobOperation))}
    blank = lambda: {"worked": 0.0, "downtime": 0.0, "earned": 0.0, "earned_good": 0.0, "good": 0.0, "scrap": 0.0}  # noqa: E731
    cell = defaultdict(blank)  # (stage_id, week) -> sums

    for e in db.scalars(select(StageEvent).where(StageEvent.created_at >= since,
                                                 StageEvent.action.in_(("start", "complete", "scrap")))):
        op = ops.get(e.job_operation_id)
        if not op:
            continue
        m = cell[(op.stage_id, week_start(e.created_at))]
        if e.action == "start":
            m["worked"] += e.minutes
            if e.minutes:  # first start carries the setup; earn its standard setup too
                m["earned"] += op.setup_minutes
                m["earned_good"] += op.setup_minutes
        elif e.action == "complete":
            m["worked"] += e.minutes
            m["earned"] += op.run_minutes_per_unit * e.qty
            m["earned_good"] += op.run_minutes_per_unit * e.qty
            m["good"] += e.qty
        else:
            m["earned"] += op.run_minutes_per_unit * e.qty
            m["scrap"] += e.qty

    issues = list(db.scalars(select(IssueLog).where(IssueLog.created_at >= since)))
    for i in issues:
        if i.stage_id and i.minutes_lost:
            cell[(i.stage_id, week_start(i.created_at))]["downtime"] += i.minutes_lost

    cap_week = {s.id: s.capacity_hours_per_day * 60 * WORKING_DAYS for s in stages}
    # the current week is partial: count capacity only for the working days so far
    days_in = [min(WORKING_DAYS, (today - wk).days + 1) if wk == week_start(today) else WORKING_DAYS
               for wk in week_keys]
    period_weeks = sum(days_in) / WORKING_DAYS
    weekly = []
    for idx, wk in enumerate(week_keys):
        tot = blank()
        for s in stages:
            for k, v in cell[(s.id, wk)].items():
                tot[k] += v
        days = days_in[idx]
        cap = sum(cap_week.values()) * days / WORKING_DAYS
        weekly.append({"week": wk.isoformat(), "index": idx, "partial": days < WORKING_DAYS, **_metrics(tot, cap)})

    full = [w for w in weekly if not w["partial"]]
    trend = _trend([(w["index"], w["output_vs_capacity"]) for w in full])
    oee_trend = _trend([(w["index"], w["oee"]) for w in full])

    by_stage = []
    for s in stages:
        tot = blank()
        for wk in week_keys:
            for k, v in cell[(s.id, wk)].items():
                tot[k] += v
        cap = cap_week[s.id] * period_weeks
        by_stage.append({"stage_id": s.id, "stage": s.name, "code": s.code, "color": s.color,
                         "rate_per_hour": s.rate_per_hour, **_metrics(tot, cap or 1)})

    overall_tot = blank()
    for (sid, wk), m in cell.items():
        if wk in week_keys:
            for k, v in m.items():
                overall_tot[k] += v
    overall = _metrics(overall_tot, sum(cap_week.values()) * period_weeks)

    # downtime by cause
    stage_by_id = {s.id: s for s in stages}
    pareto = defaultdict(lambda: {"minutes": 0.0, "cost": 0.0, "count": 0})
    for i in issues:
        if i.minutes_lost and i.stage_id:
            key = (i.category, stage_by_id[i.stage_id].name)
            p = pareto[key]
            p["minutes"] += i.minutes_lost
            p["cost"] += i.minutes_lost / 60 * stage_by_id[i.stage_id].rate_per_hour
            p["count"] += 1
    downtime = sorted(({"category": c, "stage": st, "hours": round(v["minutes"] / 60, 2), "cost": round(v["cost"], 2),
                        "count": v["count"]} for (c, st), v in pareto.items()), key=lambda r: -r["cost"])

    costs = cost_report(db, since, datetime.combine(week_start(today) - timedelta(weeks=3), datetime.min.time()))
    return {"weeks": weekly, "trend": trend, "oee_trend": oee_trend, "overall": overall, "by_stage": by_stage,
            "downtime": downtime, "costs": costs,
            "issues": {"open": sum(1 for i in db.scalars(select(IssueLog).where(IssueLog.status == "open"))),
                       "logged": len(issues)},
            "opportunities": opportunities(db, by_stage, downtime, costs, period_weeks or 1)}


def cost_report(db: Session, since: datetime, recent_from: datetime) -> dict:
    """Average actual unit cost vs BC standard per product, from closed jobs.
    recent_avg covers jobs closed from recent_from (last 4 weeks), earlier_avg before it."""
    prod = defaultdict(lambda: {"jobs": 0, "units": 0.0, "actual": 0.0, "bc_value": 0.0, "std": 0.0, "recipe_std": 0.0,
                                "material": 0.0, "conversion": 0.0, "scrap": 0.0, "last4": [0.0, 0.0], "prev": [0.0, 0.0]})
    materials = defaultdict(float)
    stage_var = defaultdict(float)
    jobs = db.scalars(select(Job).where(Job.status == "completed", Job.completed_at >= since))
    for j in jobs:
        c = j.cost_snapshot
        if not c or j.qty_good <= 0:
            continue
        p = prod[j.item_id]
        p["item_no"], p["description"] = j.item.no, j.item.description
        p["jobs"] += 1
        p["units"] += j.qty_good
        p["scrap"] += j.qty_scrap
        p["actual"] += c["total_cost"]
        p["bc_value"] += c["bc_value"]
        p["std"] = c["output_unit_cost"]
        p["recipe_std"] = c["std_unit_cost"]
        p["material"] += c["variance"]["material_usage"]
        p["conversion"] += c["variance"]["conversion"]
        bucket = p["last4"] if j.completed_at.replace(tzinfo=None) >= recent_from else p["prev"]
        bucket[0] += c["total_cost"]
        bucket[1] += j.qty_good
        for m in c["materials"]:
            materials[m["item_no"]] += m["variance"]
        for s in c["stages"]:
            stage_var[s["stage"]] += s["variance"]
    rows = []
    for p in prod.values():
        avg = p["actual"] / p["units"]
        rows.append({"item_no": p["item_no"], "description": p["description"], "jobs": p["jobs"],
                     "units": p["units"], "scrap": p["scrap"], "avg_unit_cost": round(avg, 2),
                     "bc_standard": round(p["std"], 2), "recipe_standard": round(p["recipe_std"], 2),
                     "variance_pct": round((avg - p["std"]) / p["std"], 4) if p["std"] else None,
                     "variance": round(p["actual"] - p["bc_value"], 2),
                     "material_variance": round(p["material"], 2), "conversion_variance": round(p["conversion"], 2),
                     "recent_avg": round(p["last4"][0] / p["last4"][1], 2) if p["last4"][1] else None,
                     "earlier_avg": round(p["prev"][0] / p["prev"][1], 2) if p["prev"][1] else None})
    rows.sort(key=lambda r: r["item_no"])
    return {"products": rows,
            "total_variance": round(sum(r["variance"] for r in rows), 2),
            "material_variance_by_item": {k: round(v, 2) for k, v in sorted(materials.items(), key=lambda kv: -kv[1])},
            "conversion_variance_by_stage": {k: round(v, 2) for k, v in sorted(stage_var.items(), key=lambda kv: -kv[1])}}


def opportunities(db: Session, by_stage: list, downtime: list, costs: dict, weeks: float) -> list[dict]:
    """Ranked improvement opportunities with the cost at stake over the period
    and annualised (x 52 / weeks)."""
    ops = []
    annual = 52 / weeks

    def add(area, title, detail, impact):
        if impact > 1:
            ops.append({"area": area, "title": title, "detail": detail, "impact": round(impact, 2),
                        "annualised": round(impact * annual, 0)})

    for stage, var in costs["conversion_variance_by_stage"].items():
        s = next((x for x in by_stage if x["stage"] == stage), None)
        perf = s["performance"] if s else None
        if var > 0:
            add("Labour", f"{stage} runs slower than standard",
                f"Performance {perf:.0%} of standard time. Review method, fixtures and the standard minutes in the recipe."
                if perf else "Review method and the standard minutes in the recipe.", var)
    for d in downtime[:4]:
        add("Downtime", f"{d['category'].capitalize()} at {d['stage']}",
            f"{d['hours']:.1f} h lost across {d['count']} logged issues. Target the root cause on the Issues log.", d["cost"])
    for item_no, var in list(costs["material_variance_by_item"].items())[:3]:
        if var > 0:
            add("Material", f"{item_no} used above standard",
                "Scrap and over-use against the recipe. Check nesting, cut lists and scrap reasons.", var)
    for p in costs["products"]:
        gap = (p["recipe_standard"] - p["bc_standard"]) * p["units"]
        if p["bc_standard"] and abs(p["recipe_standard"] - p["bc_standard"]) / p["bc_standard"] > 0.02:
            add("Standards", f"Update BC standard cost for {p['item_no']}",
                f"Recipe works out at {p['recipe_standard']:.2f} vs BC {p['bc_standard']:.2f}. "
                "Inventory and margins are misstated until BC is updated.", abs(gap))
    oees = [s for s in by_stage if s["oee"] is not None]
    if oees:
        avg = sum(s["oee"] for s in oees) / len(oees)
        worst = min(oees, key=lambda s: s["oee"])
        if worst["oee"] < avg - 0.05:
            lost = (avg - worst["oee"]) * worst["worked_hours"] * worst["rate_per_hour"]
            add("OEE", f"Lift {worst['stage']} OEE to the shop average",
                f"{worst['stage']} OEE {worst['oee']:.0%} vs {avg:.0%} average "
                f"(availability {worst['availability']:.0%}, performance {min(worst['performance'], 1):.0%}, "
                f"quality {worst['quality']:.0%}).", lost)
    ops.sort(key=lambda o: -o["impact"])
    return ops
