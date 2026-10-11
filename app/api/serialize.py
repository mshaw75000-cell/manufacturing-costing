from ..models import Demand, Item, Job, Recipe, Stage
from ..services.costing import job_costs, standard_unit_cost


def item(i: Item) -> dict:
    return {"id": i.id, "no": i.no, "description": i.description, "item_type": i.item_type,
            "uom": i.uom, "unit_cost": i.unit_cost, "qty_on_hand": i.qty_on_hand,
            "lot_tracked": i.lot_tracked, "bc_synced_at": i.bc_synced_at}


def stage(s: Stage) -> dict:
    return {"id": s.id, "code": s.code, "name": s.name, "sequence": s.sequence,
            "rate_per_hour": s.rate_per_hour, "color": s.color}


def recipe(r: Recipe, active: dict[int, Recipe] | None = None) -> dict:
    return {
        "id": r.id, "item_id": r.item_id, "item_no": r.item.no, "item_description": r.item.description,
        "version": r.version, "output_qty": r.output_qty, "std_batch_qty": r.std_batch_qty, "active": r.active, "notes": r.notes,
        "operations": [{"id": o.id, "stage_id": o.stage_id, "stage_code": o.stage.code,
                        "stage_name": o.stage.name, "sequence": o.sequence,
                        "setup_minutes": o.setup_minutes, "run_minutes_per_unit": o.run_minutes_per_unit}
                       for o in r.operations],
        "components": [{"id": c.id, "item_id": c.item_id, "item_no": c.item.no,
                        "description": c.item.description, "uom": c.item.uom, "qty_per": c.qty_per,
                        "scrap_pct": c.scrap_pct, "operation_seq": c.operation_seq,
                        "unit_cost": c.item.unit_cost}
                       for c in r.components],
        "standard_unit_cost": round(standard_unit_cost(r, active or {}), 4),
    }


def demand(d: Demand) -> dict:
    return {"id": d.id, "item_id": d.item_id, "item_no": d.item.no, "description": d.item.description,
            "qty": d.qty, "due_date": d.due_date, "source": d.source, "reference": d.reference,
            "status": d.status, "created_at": d.created_at}


def job(j: Job, detail: bool = False) -> dict:
    out = {
        "id": j.id, "number": j.number, "item_id": j.item_id, "item_no": j.item.no, "recipe_id": j.recipe_id,
        "description": j.item.description, "qty": j.qty, "status": j.status, "due_date": j.due_date,
        "demand_id": j.demand_id, "qty_good": j.qty_good, "qty_scrap": j.qty_scrap,
        "created_at": j.created_at, "completed_at": j.completed_at,
        "costs": j.cost_snapshot if j.status == "completed" and j.cost_snapshot else job_costs(j),
        "operations": [{
            "id": o.id, "sequence": o.sequence, "stage_id": o.stage_id, "stage_code": o.stage.code,
            "stage_name": o.stage.name, "color": o.stage.color,
            "qty_queued": o.qty_queued, "qty_in_process": o.qty_in_process,
            "qty_done": o.qty_done, "qty_scrap": o.qty_scrap, "actual_minutes": round(o.actual_minutes, 2),
            "started_at": o.started_at, "completed_at": o.completed_at,
        } for o in j.operations],
    }
    if detail:
        out["issues"] = [{"item_no": i.item.no, "description": i.item.description, "qty": i.qty,
                          "lot_id": i.lot_id, "lot_no": i.lot.lot_no if i.lot else "",
                          "heat_no": i.lot.heat_no if i.lot else "",
                          "uom": i.item.uom, "unit_cost": i.unit_cost, "value": round(i.qty * i.unit_cost, 4),
                          "job_operation_id": i.job_operation_id, "created_at": i.created_at}
                         for i in j.issues]
    return out


def lot(l) -> dict:
    return {"id": l.id, "item_id": l.item_id, "item_no": l.item.no, "description": l.item.description,
            "uom": l.item.uom, "lot_no": l.lot_no, "heat_no": l.heat_no, "supplier": l.supplier,
            "cert_ref": l.cert_ref, "source": l.source, "job_id": l.job_id,
            "qty_received": l.qty_received, "qty_on_hand": l.qty_on_hand, "notes": l.notes,
            "created_at": l.created_at}
