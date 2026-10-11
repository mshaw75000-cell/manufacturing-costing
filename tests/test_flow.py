import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import db as dbmod
from app.bc.mock import MockBusinessCentral
from app.bc.sync import set_bc
from app.main import app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(dbmod, "engine", engine)
    monkeypatch.setattr(dbmod, "SessionLocal", Session)
    import app.seed as seed_mod
    import app.main as main_mod
    monkeypatch.setattr(seed_mod, "engine", engine)
    monkeypatch.setattr(seed_mod, "SessionLocal", Session)
    monkeypatch.setattr(main_mod, "engine", engine)
    set_bc(MockBusinessCentral(str(tmp_path / "bc.json")))

    def override():
        s = Session()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[dbmod.get_db] = override
    seed_mod.seed()
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
    set_bc(None)


def by_no(rows, key="item_no"):
    return {r[key]: r for r in rows}


def test_plan_explodes_multi_level(client):
    plan = by_no(client.get("/api/plan").json()["lines"])
    # 24 frames demanded, none on hand -> make 24
    assert plan["FG-FRAME-01"]["action"] == "make" and plan["FG-FRAME-01"]["net"] == 24
    # 96 brackets needed -> 5 batches of 20 = 100
    assert plan["SA-BRKT-01"]["gross"] == 96 and plan["SA-BRKT-01"]["net"] == 100
    # 5 sheets x 1.05 scrap = 5.25, 40 on hand -> ok
    assert plan["RM-STL-3MM"]["gross"] == pytest.approx(5.25) and plan["RM-STL-3MM"]["action"] == "ok"
    # Powder 24 * 0.6 * 1.1 = 15.84 kg, 25 on hand -> ok; tube 24*1.5*1.03 = 37.08, 60 on hand -> ok
    assert plan["RM-PWD-BLK"]["gross"] == pytest.approx(15.84)


def test_plan_shows_shortfall(client):
    items = by_no(client.get("/api/items").json(), "no")
    client.post("/api/demand", json={"item_id": items["FG-FRAME-01"]["id"], "qty": 30})
    plan = by_no(client.get("/api/plan").json()["lines"])
    # 54 frames * 0.6 * 1.1 = 35.64 kg powder vs 25 on hand
    assert plan["RM-PWD-BLK"]["action"] == "buy"
    assert plan["RM-PWD-BLK"]["net"] == pytest.approx(10.64)


def test_jobs_from_plan_net_to_zero(client):
    jobs = client.post("/api/plan/create-jobs").json()
    assert sorted(j["item_no"] for j in jobs) == ["FG-FRAME-01", "SA-BRKT-01"]
    plan = client.get("/api/plan").json()
    assert plan["summary"]["make_lines"] == 0


def run_job(client, job, qty, scrap_at=None):
    jid = job["id"]
    client.post(f"/api/jobs/{jid}/release").raise_for_status()
    for op in client.get(f"/api/jobs/{jid}").json()["operations"]:
        q = client.get(f"/api/jobs/{jid}").json()
        cur = next(o for o in q["operations"] if o["id"] == op["id"])
        n = cur["qty_queued"]
        r = client.post(f"/api/jobs/{jid}/start", json={"operation_id": op["id"], "qty": n})
        assert r.status_code == 200, r.text
        if scrap_at == op["stage_code"]:
            client.post(f"/api/jobs/{jid}/scrap", json={"operation_id": op["id"], "qty": 1}).raise_for_status()
            n -= 1
        client.post(f"/api/jobs/{jid}/complete", json={"operation_id": op["id"], "qty": n}).raise_for_status()
    return client.post(f"/api/jobs/{jid}/finish")


def test_full_flow_costs_and_posts_to_bc(client):
    items = by_no(client.get("/api/items").json(), "no")
    brkt = client.post("/api/jobs", json={"item_id": items["SA-BRKT-01"]["id"], "qty": 100}).json()
    r = run_job(client, brkt, 100)
    assert r.status_code == 200, r.text
    b = r.json()
    # material: 5 sheets * 1.05 * 185 = 971.25
    assert b["costs"]["material_cost"] == pytest.approx(971.25)
    # conversion: CUT (15 + 50 min) @85 + PRESS (30 + 75 min) @110
    conv = 65 / 60 * 85 + 105 / 60 * 110
    assert b["costs"]["conversion_cost"] == pytest.approx(conv, abs=1e-3)
    assert b["costs"]["unit_cost"] == pytest.approx((971.25 + conv) / 100, abs=1e-3)

    items = by_no(client.get("/api/items").json(), "no")
    assert items["SA-BRKT-01"]["qty_on_hand"] == 100
    assert items["RM-STL-3MM"]["qty_on_hand"] == pytest.approx(40 - 5.25)

    demand = client.get("/api/demand").json()[0]
    frame = client.post(f"/api/demand/{demand['id']}/create-job").json()
    f = run_job(client, frame, 24, scrap_at="WELD").json()
    assert f["qty_good"] == 23 and f["qty_scrap"] == 1
    assert f["status"] == "completed"
    # brackets consumed at weld from the bracket job's output, valued at its unit cost
    assert by_no(client.get("/api/items").json(), "no")["SA-BRKT-01"]["qty_on_hand"] == 4
    assert client.get("/api/demand").json() == []  # demand fulfilled

    res = client.post("/api/bc/process-postings").json()
    assert res.get("failed") == 0 and res["posted"] > 0, res
    client.post("/api/bc/sync-items").raise_for_status()
    items = by_no(client.get("/api/items").json(), "no")
    assert items["FG-FRAME-01"]["qty_on_hand"] == 23
    assert items["FG-FRAME-01"]["unit_cost"] == pytest.approx(f["costs"]["unit_cost"], abs=1e-3)
    assert items["RM-STL-3MM"]["qty_on_hand"] == pytest.approx(40 - 5.25)


def test_start_blocks_on_shortage_unless_forced(client):
    items = by_no(client.get("/api/items").json(), "no")
    job = client.post("/api/jobs", json={"item_id": items["FG-FRAME-01"]["id"], "qty": 10}).json()
    client.post(f"/api/jobs/{job['id']}/release")
    ops = client.get(f"/api/jobs/{job['id']}").json()["operations"]
    client.post(f"/api/jobs/{job['id']}/start", json={"operation_id": ops[0]["id"], "qty": 10}).raise_for_status()
    client.post(f"/api/jobs/{job['id']}/complete", json={"operation_id": ops[0]["id"], "qty": 10}).raise_for_status()
    r = client.post(f"/api/jobs/{job['id']}/start", json={"operation_id": ops[1]["id"], "qty": 10})
    assert r.status_code == 409  # no brackets on hand
    assert r.json()["detail"]["shortages"][0]["item_no"] == "SA-BRKT-01"
    r = client.post(f"/api/jobs/{job['id']}/start", json={"operation_id": ops[1]["id"], "qty": 10, "force": True})
    assert r.status_code == 200


def test_wip_board_shows_material_at_stage(client):
    items = by_no(client.get("/api/items").json(), "no")
    job = client.post("/api/jobs", json={"item_id": items["FG-FRAME-01"]["id"], "qty": 5}).json()
    client.post(f"/api/jobs/{job['id']}/release")
    ops = client.get(f"/api/jobs/{job['id']}").json()["operations"]
    client.post(f"/api/jobs/{job['id']}/start", json={"operation_id": ops[0]["id"], "qty": 3})
    wip = {s["code"]: s for s in client.get("/api/wip").json()["stages"]}
    cut = wip["CUT"]["jobs"][0]
    assert cut["qty_queued"] == 2 and cut["qty_in_process"] == 3
    assert cut["materials"][0]["item_no"] == "RM-TUBE-50"
    assert cut["materials"][0]["qty"] == pytest.approx(3 * 1.5 * 1.03)


def test_recipe_cycle_rejected(client):
    items = by_no(client.get("/api/items").json(), "no")
    stage = client.get("/api/stages").json()[0]
    r = client.post("/api/recipes", json={
        "item_id": items["RM-STL-3MM"]["id"], "operations": [{"stage_id": stage["id"], "sequence": 10}],
        "components": [{"item_id": items["FG-FRAME-01"]["id"], "qty_per": 1}]})
    assert r.status_code == 422


def test_standard_cost_rolls_up_sub_assemblies(client):
    recipes = {r["item_no"]: r for r in client.get("/api/recipes").json()}
    brkt = recipes["SA-BRKT-01"]["standard_unit_cost"]
    # sheet 185 * 1.05 + cut (15 + 0.5*20) min @85 + press (30 + 0.75*20) min @110, per 20 brackets
    assert brkt == pytest.approx((185 * 1.05 + 25 / 60 * 85 + 45 / 60 * 110) / 20, abs=1e-3)
    frame = recipes["FG-FRAME-01"]["standard_unit_cost"]
    assert frame > 4 * brkt
