"""Export the trial dataset in the shape demo/mockserver.js keeps in the browser.

Builds the trial data (app/trial_data.py) in a throwaway database, then dumps
every table to JSON. The demo page shifts all dates forward on first load so
the data always looks current.
"""
import json
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path

from sqlalchemy import create_engine, inspect, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.bc.mock import MockBusinessCentral
from app.db import Base
from app import models as M
from app.trial_data import build_trial


def _val(v):
    if isinstance(v, datetime):
        return (v if v.tzinfo else v.replace(tzinfo=timezone.utc)).isoformat().replace("+00:00", "Z")
    if isinstance(v, date):
        return v.isoformat()
    return v


def _row(obj) -> dict:
    return {c.key: _val(getattr(obj, c.key)) for c in inspect(type(obj)).column_attrs}


def export(now: datetime | None = None, with_reports: bool = False) -> dict:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, expire_on_commit=False)()
    with tempfile.TemporaryDirectory() as tmp:
        bc = MockBusinessCentral(str(Path(tmp) / "bc.json"))
        now = now or datetime.now(timezone.utc)
        build_trial(db, bc, now=now)
        bc_state = bc.load_state()

    def rows(model, order=None):
        return [_row(o) for o in db.scalars(select(model).order_by(order or model.id))]

    recipes = []
    for r in db.scalars(select(M.Recipe).order_by(M.Recipe.id)):
        d = _row(r)
        d["operations"] = [_row(o) for o in r.operations]
        d["components"] = [_row(c) for c in r.components]
        recipes.append(d)
    jobs = []
    for j in db.scalars(select(M.Job).order_by(M.Job.id)):
        d = _row(j)
        d["operations"] = [_row(o) for o in j.operations]
        jobs.append(d)
    prs = []
    for pr in db.scalars(select(M.PurchaseRequest).order_by(M.PurchaseRequest.id)):
        d = _row(pr)
        d["lines"] = [_row(l) for l in pr.lines]
        prs.append(d)

    state = {
        "built_at": _val(now),
        "items": rows(M.Item), "stages": rows(M.Stage), "recipes": recipes, "demands": rows(M.Demand),
        "jobs": jobs, "lots": rows(M.Lot), "issues": rows(M.MaterialIssue), "events": rows(M.StageEvent),
        "postings": [{k: v for k, v in p.items() if k != "response"} for p in rows(M.BcPosting)],
        "issue_logs": rows(M.IssueLog), "notes": rows(M.JobNote), "purchase_requests": prs,
        "bc": bc_state,
        "settings": __import__("app.services.assumptions", fromlist=["get_settings"]).get_settings(db),
    }
    seq = {k: max((r["id"] for r in v), default=0) for k, v in state.items() if isinstance(v, list)}
    seq["recipe_operations"] = max((o["id"] for r in recipes for o in r["operations"]), default=0)
    seq["recipe_components"] = max((c["id"] for r in recipes for c in r["components"]), default=0)
    seq["job_operations"] = max((o["id"] for j in jobs for o in j["operations"]), default=0)
    seq["purchase_request_lines"] = max((l["id"] for p in prs for l in p["lines"]), default=0)
    state["seq"] = seq
    if with_reports:  # for parity checks against the in-browser port
        from app.services.planning import build_plan
        from app.services.purchasing import availability
        from app.services.reports import performance_report
        from app.services.costing import job_costs
        open_job = db.scalar(select(M.Job).where(M.Job.status == "in_progress").order_by(M.Job.id))
        state["_python"] = json.loads(json.dumps({
            "plan": build_plan(db), "availability": availability(db), "report": performance_report(db),
            "job": {"id": open_job.id, "costs": job_costs(open_job)},
            "assumptions": __import__("app.services.assumptions", fromlist=["summary"]).summary(db),
            "breakdowns": {r.item.no: __import__("app.services.assumptions", fromlist=["x"]).standard_breakdown(
                r, __import__("app.services.costing", fromlist=["x"]).active_recipes(db),
                __import__("app.services.assumptions", fromlist=["x"]).get_settings(db))
                for r in db.scalars(select(M.Recipe))}}, default=str))
    return state


if __name__ == "__main__":
    s = export()
    print(len(json.dumps(s)), "bytes;", {k: len(v) for k, v in s.items() if isinstance(v, list)})
