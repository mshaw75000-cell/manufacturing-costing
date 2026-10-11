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


def test_full_flow_standard_costing_and_bc(client):
    items = by_no(client.get("/api/items").json(), "no")
    brkt = client.post("/api/jobs", json={"item_id": items["SA-BRKT-01"]["id"], "qty": 100}).json()
    r = run_job(client, brkt, 100)
    assert r.status_code == 200, r.text
    c = r.json()["costs"]
    # material at standard: 5 sheets * 1.05 * 185 = 971.25
    assert c["material_cost"] == pytest.approx(971.25)
    # conversion: CUT (15 + 50 min) @85 + PRESS (30 + 75 min) @110
    conv = 65 / 60 * 85 + 105 / 60 * 110
    assert c["conversion_cost"] == pytest.approx(conv, abs=1e-3)
    assert c["unit_cost"] == pytest.approx((971.25 + conv) / 100, abs=1e-3)
    # output went to BC at the BC standard (15.50), not at actual cost
    assert c["output_unit_cost"] == 15.50 and c["bc_value"] == pytest.approx(1550)
    v = c["variance"]
    assert v["total"] == pytest.approx(c["total_cost"] - 1550, abs=1e-3)
    assert v["material_usage"] + v["conversion"] + v["standard_gap"] == pytest.approx(v["total"], abs=1e-3)
    # ran exactly to standard -> no usage or conversion variance
    assert v["material_usage"] == pytest.approx(0, abs=1e-3) and v["conversion"] == pytest.approx(0, abs=1e-3)

    items = by_no(client.get("/api/items").json(), "no")
    assert items["SA-BRKT-01"]["qty_on_hand"] == 100
    assert items["RM-STL-3MM"]["qty_on_hand"] == pytest.approx(40 - 5.25)

    demand = client.get("/api/demand").json()[0]
    frame = client.post(f"/api/demand/{demand['id']}/create-job").json()
    f = run_job(client, frame, 24, scrap_at="WELD").json()
    assert f["qty_good"] == 23 and f["qty_scrap"] == 1
    assert f["status"] == "completed"
    # brackets issued at their standard cost
    brk = next(m for m in f["costs"]["materials"] if m["item_no"] == "SA-BRKT-01")
    assert brk["std_cost"] == 15.50 and brk["actual_qty"] == 96 and brk["std_qty"] == 92
    assert brk["variance"] == pytest.approx(4 * 15.50)  # the scrapped frame's brackets
    assert by_no(client.get("/api/items").json(), "no")["SA-BRKT-01"]["qty_on_hand"] == 4
    assert client.get("/api/demand").json() == []  # demand fulfilled

    res = client.post("/api/bc/process-postings").json()
    assert res.get("failed") == 0 and res["posted"] > 0, res
    client.post("/api/bc/sync-items").raise_for_status()
    items = by_no(client.get("/api/items").json(), "no")
    assert items["FG-FRAME-01"]["qty_on_hand"] == 23
    assert items["FG-FRAME-01"]["unit_cost"] == 245.00  # standard is never changed by postings
    assert items["RM-STL-3MM"]["qty_on_hand"] == pytest.approx(40 - 5.25)


def test_partial_output_then_close_short(client):
    items = by_no(client.get("/api/items").json(), "no")
    job = client.post("/api/jobs", json={"item_id": items["SA-BRKT-01"]["id"], "qty": 40}).json()
    jid = job["id"]
    client.post(f"/api/jobs/{jid}/release")
    cut, press = client.get(f"/api/jobs/{jid}").json()["operations"]
    move = lambda act, op, q: client.post(f"/api/jobs/{jid}/{act}", json={"operation_id": op["id"], "qty": q})  # noqa
    move("start", cut, 40).raise_for_status()
    move("complete", cut, 40).raise_for_status()
    move("start", press, 15).raise_for_status()
    move("complete", press, 15).raise_for_status()

    outputs = [p for p in client.get("/api/bc/postings").json() if p["kind"] == "output"]
    assert [(p["qty"], p["unit_cost"], p["lot_no"]) for p in outputs] == [(15, 15.50, job["number"])]
    assert by_no(client.get("/api/items").json(), "no")["SA-BRKT-01"]["qty_on_hand"] == 15

    move("start", press, 10).raise_for_status()
    move("complete", press, 10).raise_for_status()
    outputs = [p for p in client.get("/api/bc/postings").json() if p["kind"] == "output"]
    assert sorted(p["qty"] for p in outputs) == [10, 15]

    # 15 still waiting at press; close the job short
    done = client.post(f"/api/jobs/{jid}/finish").json()
    assert done["status"] == "completed" and done["qty_good"] == 25
    assert all(o["qty_queued"] == 0 for o in done["operations"])
    # no extra output posted on close
    assert len([p for p in client.get("/api/bc/postings").json() if p["kind"] == "output"]) == 2


def test_lots_fifo_preferred_and_trace(client):
    items = by_no(client.get("/api/items").json(), "no")
    lots = {l["lot_no"]: l for l in client.get(f"/api/lots?item_id={items['RM-STL-3MM']['id']}").json()}
    # Bracket job A: 400 brackets = 21 sheets, operator picks the newer heat first
    a = client.post("/api/jobs", json={"item_id": items["SA-BRKT-01"]["id"], "qty": 400}).json()
    client.post(f"/api/jobs/{a['id']}/release")
    cut = client.get(f"/api/jobs/{a['id']}").json()["operations"][0]
    r = client.post(f"/api/jobs/{a['id']}/start", json={
        "operation_id": cut["id"], "qty": 400,
        "lot_choices": {str(items["RM-STL-3MM"]["id"]): lots["L24-0930"]["id"]}})
    assert r.status_code == 200, r.text
    issues = r.json()["issues"]
    assert [(i["lot_no"], i["heat_no"], i["qty"]) for i in issues] == [
        ("L24-0930", "H-779455", 20), ("L24-0911", "H-778120", pytest.approx(1))]
    after = {l["lot_no"]: l["qty_on_hand"] for l in client.get("/api/lots?q=L24").json()}
    assert after["L24-0930"] == 0 and after["L24-0911"] == pytest.approx(19)

    # finish brackets, then make frames from them
    jid = a["id"]
    client.post(f"/api/jobs/{jid}/complete", json={"operation_id": cut["id"], "qty": 400})
    press = client.get(f"/api/jobs/{jid}").json()["operations"][1]
    client.post(f"/api/jobs/{jid}/start", json={"operation_id": press["id"], "qty": 400})
    client.post(f"/api/jobs/{jid}/complete", json={"operation_id": press["id"], "qty": 400})
    frame = client.post("/api/jobs", json={"item_id": items["FG-FRAME-01"]["id"], "qty": 2}).json()
    run_job(client, frame, 2).raise_for_status()

    fg_lot = client.get(f"/api/lots?q={frame['number']}").json()[0]
    assert fg_lot["item_no"] == "FG-FRAME-01" and fg_lot["qty_on_hand"] == 2
    t = client.get(f"/api/lots/{fg_lot['id']}/trace").json()
    # backward: frame lot -> bracket lot -> both steel heats
    brkt_node = next(c for c in t["backward"]["children"] if c["item_no"] == "SA-BRKT-01")
    assert brkt_node["lot_no"] == a["number"] and brkt_node["qty"] == 8
    assert {c["heat_no"] for c in brkt_node["children"]} == {"H-779455", "H-778120"}
    # powder came from the oldest powder batch
    pwd = next(c for c in t["backward"]["children"] if c["item_no"] == "RM-PWD-BLK")
    assert pwd["lot_no"] == "PB-6611"

    # forward: heat H-779455 -> bracket job -> frame lot
    heat = client.get("/api/lots?q=H-779455").json()[0]
    fwd = client.get(f"/api/lots/{heat['id']}/trace").json()["forward"]
    assert fwd["children"][0]["lot_no"] == a["number"]
    assert fwd["children"][0]["children"][0]["lot_no"] == frame["number"]


def test_receive_lot(client):
    items = by_no(client.get("/api/items").json(), "no")
    body = {"item_id": items["RM-STL-3MM"]["id"], "lot_no": "L24-1010", "heat_no": "H-781001", "qty": 30,
            "supplier": "BlueScope", "cert_ref": "MC-56000"}
    assert client.post("/api/lots", json=body).status_code == 200
    assert client.post("/api/lots", json=body).status_code == 409
    assert client.get("/api/lots?q=H-781001").json()[0]["qty_on_hand"] == 30


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
    # the frame rolls up brackets at their BC standard (15.50), not the recipe figure
    frame = recipes["FG-FRAME-01"]["standard_unit_cost"]
    tube, wire, powder, bolts = 1.5 * 1.03 * 72.5, 0.2 * 9.8, 0.6 * 1.1 * 21, 8 * 0.45
    # setup spread over the standard run of 24
    conv = (10 / 24 + 4) / 60 * 85 + (15 / 24 + 18) / 60 * 95 + (20 / 24 + 6) / 60 * 120 + 5 / 60 * 75
    assert frame == pytest.approx(tube + 4 * 15.50 + wire + powder + bolts + conv, abs=1e-3)
