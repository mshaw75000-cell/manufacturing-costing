import pytest

from tests.test_flow import by_no, client, run_job  # noqa: F401  (fixture)


def test_shortage_to_purchase_order_to_receipt(client):
    items = by_no(client.get("/api/items").json(), "no")
    client.post("/api/demand", json={"item_id": items["FG-FRAME-01"]["id"], "qty": 30})
    avail = by_no(client.get("/api/materials/availability").json())
    assert avail["RM-PWD-BLK"]["status"] == "short" and avail["RM-PWD-BLK"]["shortage"] == pytest.approx(10.64)

    prs = client.post("/api/purchase-requests/from-shortages").json()
    # one request per vendor: tube (Orrcon) and powder (Dulux) are both short
    assert sorted(p["vendor_no"] for p in prs) == ["V-1002", "V-1003"]
    prs = [p for p in prs if p["vendor_no"] == "V-1003"]
    assert prs[0]["status"] == "draft"
    assert prs[0]["lines"][0]["qty"] == pytest.approx(10.7)  # KG rounded up to 0.1
    # an open request counts as supply, so the plan no longer asks to buy powder
    plan = by_no(client.get("/api/plan").json()["lines"])
    assert plan["RM-PWD-BLK"]["action"] == "ok"
    assert client.post("/api/purchase-requests/from-shortages").json() == []

    sent = client.post(f"/api/purchase-requests/{prs[0]['id']}/send").json()
    assert sent["status"] == "sent" and sent["bc_po_no"].startswith("PO-")
    before = items["RM-PWD-BLK"]["qty_on_hand"]
    line_id = sent["lines"][0]["id"]
    got = client.post(f"/api/purchase-requests/{prs[0]['id']}/receive", json={"heat_numbers": {str(line_id): "B-9001"}}).json()
    assert got["status"] == "received"
    assert by_no(client.get("/api/items").json(), "no")["RM-PWD-BLK"]["qty_on_hand"] == pytest.approx(before + 10.7)
    lot = client.get(f"/api/lots?q={sent['bc_po_no']}").json()[0]
    assert lot["heat_no"] == "B-9001" and lot["qty_on_hand"] == pytest.approx(10.7)
    # BC agrees after a pull
    client.post("/api/bc/sync-items")
    assert by_no(client.get("/api/items").json(), "no")["RM-PWD-BLK"]["qty_on_hand"] == pytest.approx(before + 10.7)


def test_notes_and_issue_log(client):
    items = by_no(client.get("/api/items").json(), "no")
    job = client.post("/api/jobs", json={"item_id": items["FG-FRAME-01"]["id"], "qty": 5}).json()
    client.post(f"/api/jobs/{job['id']}/release")
    op = client.get(f"/api/jobs/{job['id']}").json()["operations"][0]
    client.post(f"/api/jobs/{job['id']}/start", json={"operation_id": op["id"], "qty": 5})
    n = client.post(f"/api/jobs/{job['id']}/notes", json={"note": "Tube ends burred", "detail": "Deburr before weld",
                                                           "operation_id": op["id"], "operator": "Sam"}).json()
    assert n["stage"] == "Cutting"
    iss = client.post("/api/issues", json={"title": "Saw blade snapped", "category": "breakdown", "job_id": job["id"],
                                           "operation_id": op["id"], "minutes_lost": 45, "operator": "Sam"}).json()
    assert iss["number"].startswith("ISS") and iss["stage"] == "Cutting" and iss["status"] == "open"
    card = client.get("/api/wip").json()["stages"][0]["jobs"][0]
    assert card["open_issues"] == 1 and card["notes"] == 1
    detail = client.get(f"/api/jobs/{job['id']}").json()
    assert detail["notes"][0]["note"] == "Tube ends burred" and detail["issue_logs"][0]["minutes_lost"] == 45
    client.post(f"/api/issues/{iss['id']}/resolve", json={"resolution": "Blade replaced"})
    assert client.get("/api/issues?status=open").json() == []


def test_performance_report(client):
    items = by_no(client.get("/api/items").json(), "no")
    brkt = client.post("/api/jobs", json={"item_id": items["SA-BRKT-01"]["id"], "qty": 100}).json()
    run_job(client, brkt, 100).raise_for_status()
    op = client.get(f"/api/jobs/{brkt['id']}").json()["operations"][1]
    client.post("/api/issues", json={"title": "Die change overran", "category": "changeover", "job_id": brkt["id"],
                                     "operation_id": op["id"], "minutes_lost": 30})
    r = client.get("/api/reports/performance").json()
    press = next(s for s in r["by_stage"] if s["code"] == "PRESS")
    # ran to standard: performance and quality 100%; 30 min downtime on 105 worked
    assert press["performance"] == pytest.approx(1) and press["quality"] == pytest.approx(1)
    assert press["availability"] == pytest.approx(105 / 135, abs=1e-3)
    assert press["oee"] == pytest.approx(105 / 135, abs=1e-3)
    assert r["downtime"][0]["category"] == "changeover"
    assert r["costs"]["products"][0]["item_no"] == "SA-BRKT-01"
    assert len(r["weeks"]) == 10
