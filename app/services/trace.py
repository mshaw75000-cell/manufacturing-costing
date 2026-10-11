"""Lot genealogy.

Backward: what went into a lot. For an output lot, the lots consumed by the
job that made it, and recursively what went into those, down to raw-material
heats. Forward: where a lot went. The jobs that consumed it, their output lots,
and recursively up to finished goods.

Genealogy is at job level: a finished lot is linked to every lot its job
consumed.
"""
from collections import defaultdict

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Job, Lot, MaterialIssue

MAX_DEPTH = 12


def _node(lot: Lot | None, qty: float, item=None, job: Job | None = None) -> dict:
    if lot is None:
        return {"lot_id": None, "lot_no": "", "heat_no": "", "item_no": item.no, "description": item.description,
                "uom": item.uom, "qty": round(qty, 4), "source": "unlotted", "job": None, "children": []}
    return {"lot_id": lot.id, "lot_no": lot.lot_no, "heat_no": lot.heat_no, "item_no": lot.item.no,
            "description": lot.item.description, "uom": lot.item.uom, "qty": round(qty, 4),
            "source": lot.source, "supplier": lot.supplier, "cert_ref": lot.cert_ref,
            "job": job.number if job else None, "children": []}


def backward(db: Session, lot: Lot, qty: float | None = None, depth: int = 0) -> dict:
    job = db.get(Job, lot.job_id) if lot.job_id else None
    node = _node(lot, lot.qty_received if qty is None else qty, job=job)
    if job and depth < MAX_DEPTH:
        consumed = defaultdict(float)
        items = {}
        for iss in job.issues:
            consumed[(iss.item_id, iss.lot_id)] += iss.qty
            items[iss.item_id] = iss.item
        for (item_id, lot_id), q in sorted(consumed.items(), key=lambda kv: items[kv[0][0]].no):
            if lot_id:
                node["children"].append(backward(db, db.get(Lot, lot_id), q, depth + 1))
            else:
                node["children"].append(_node(None, q, item=items[item_id]))
    return node


def forward(db: Session, lot: Lot, qty: float | None = None, depth: int = 0) -> dict:
    job = db.get(Job, lot.job_id) if lot.job_id else None
    node = _node(lot, lot.qty_received if qty is None else qty, job=job)
    if depth >= MAX_DEPTH:
        return node
    used = defaultdict(float)
    for iss in db.scalars(select(MaterialIssue).where(MaterialIssue.lot_id == lot.id)):
        used[iss.job_id] += iss.qty
    for job_id, q in used.items():
        consumer = db.get(Job, job_id)
        out = db.scalar(select(Lot).where(Lot.item_id == consumer.item_id, Lot.lot_no == consumer.number))
        if out:
            child = forward(db, out, depth=depth + 1)
        else:  # consuming job hasn't completed any units yet
            child = {"lot_id": None, "lot_no": "", "heat_no": "", "item_no": consumer.item.no,
                     "description": consumer.item.description, "uom": consumer.item.uom, "qty": 0,
                     "source": "wip", "job": consumer.number, "children": []}
        child["consumed_qty"] = round(q, 4)
        node["children"].append(child)
    return node
