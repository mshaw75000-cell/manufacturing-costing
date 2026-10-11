import pytest

from tests.test_flow import client  # noqa: F401  (fixture)


def test_rate_built_from_assumptions(client):
    stage = next(s for s in client.get("/api/stages").json() if s["code"] == "PCOAT")
    a = client.patch(f"/api/stages/{stage['id']}/assumptions", json={
        "crew_size": 2, "labour_rate": 50, "machine_kw": 40, "power_load_pct": 75, "gas_per_hour": 12,
        "consumables_per_hour": 4, "maintenance_per_hour": 6, "machine_cost": 384000, "machine_life_years": 10,
        "overhead_per_hour": 20, "capacity_hours_per_day": 8}).json()
    s = next(x for x in a["stages"] if x["code"] == "PCOAT")
    # electricity: 40 kW x 75% x $0.28 = 8.40; depreciation: 384000 / 10 / (8 x 5 x 48 = 1920 h) = 20.00
    assert s["breakdown"]["electricity"] == pytest.approx(8.40)
    assert s["breakdown"]["depreciation"] == pytest.approx(20.0)
    assert s["rate_per_hour"] == pytest.approx(100 + 8.4 + 12 + 4 + 6 + 20 + 20)
    assert s["annual_hours"] == 1920

    # a higher electricity price flows into the rate
    a = client.patch("/api/assumptions/settings", json={"electricity_per_kwh": 0.40}).json()
    s = next(x for x in a["stages"] if x["code"] == "PCOAT")
    assert s["breakdown"]["electricity"] == pytest.approx(12.0)
    assert s["rate_per_hour"] == pytest.approx(174.0)

    # typing a rate directly switches the stage back to a manual rate
    client.patch(f"/api/stages/{stage['id']}", json={"rate_per_hour": 120})
    s = next(x for x in client.get("/api/assumptions").json()["stages"] if x["code"] == "PCOAT")
    assert not s["rate_from_assumptions"] and s["rate_per_hour"] == 120


def test_recipe_standard_breakdown_adds_up(client):
    stage = next(s for s in client.get("/api/stages").json() if s["code"] == "WELD")
    client.patch(f"/api/stages/{stage['id']}/assumptions", json={"labour_rate": 60, "machine_kw": 10, "power_load_pct": 50})
    for r in client.get("/api/recipes").json():
        b = r["standard_breakdown"]
        assert sum(b.values()) == pytest.approx(r["standard_unit_cost"], abs=1e-3)
    frame = next(r for r in client.get("/api/recipes").json() if r["item_no"] == "FG-FRAME-01")
    # welding is 18 min/unit + 15 min setup over a run of 24 at 10 kW x 50% x $0.28
    assert frame["standard_breakdown"]["electricity"] == pytest.approx((15 / 24 + 18) / 60 * 1.4, abs=1e-4)


def test_bad_assumption_rejected(client):
    stage = client.get("/api/stages").json()[0]
    assert client.patch(f"/api/stages/{stage['id']}/assumptions", json={"labour_rate": -5}).status_code == 422
    assert client.patch("/api/assumptions/settings", json={"bogus": 1}).status_code == 422
