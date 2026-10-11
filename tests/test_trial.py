from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.bc.mock import MockBusinessCentral
from app.db import Base
from app.models import IssueLog, Item, Job, JobNote
from app.services.planning import build_plan
from app.services.reports import performance_report
from app.trial_data import build_trial


def test_trial_dataset(tmp_path):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, expire_on_commit=False)()
    bc = MockBusinessCentral(str(tmp_path / "bc.json"))
    counts = build_trial(db, bc)
    assert counts["completed_jobs"] >= 50 and counts["open_jobs"] == 20
    statuses = {s for (s,) in db.execute(select(Job.status).where(Job.status != "completed"))}
    assert statuses == {"planned", "released", "in_progress"}
    assert not [i.no for i in db.scalars(select(Item)) if i.qty_on_hand < -1e-6]
    assert db.scalar(select(func.count()).select_from(IssueLog)) > 50
    assert db.scalar(select(func.count()).select_from(IssueLog).where(IssueLog.status == "open")) > 0
    assert db.scalar(select(func.count()).select_from(JobNote)) > 10

    plan = build_plan(db)
    buys = {l["item_no"] for l in plan["lines"] if l["action"] == "buy"}
    assert "RM-CASTOR-100" in buys and plan["summary"]["make_lines"] > 0

    r = performance_report(db)
    assert 0.5 < r["overall"]["oee"] < 0.95
    assert r["trend"]["slope"] > 0  # output vs capacity improving
    assert r["opportunities"] and r["downtime"]

    # mock BC agrees with local stock and has new orders queued
    state = bc.load_state()
    assert state["incoming"] and state["demand"]
    assert any(p["status"] == "Released" for p in state["purchase_orders"])
