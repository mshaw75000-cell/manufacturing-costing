"""Material availability and purchase requests.

Shortages in the plan ("buy" lines) become purchase requests, one per vendor.
Sending a request creates a purchase order in BC. Until it's received, a
request counts as supply in the plan, so the same shortage isn't bought twice.
"""
import math
from collections import defaultdict
from datetime import date, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..bc.base import BcPurchaseLine
from ..models import Item, Job, Lot, PurchaseRequest, PurchaseRequestLine, utcnow
from .planning import OPEN_JOB_STATUSES, OPEN_PR_STATUSES, build_plan, job_remaining_components

WHOLE_UNITS = {"EA", "SHEET", "LEN", "PCS", "CTN", "BOX"}


class PurchasingError(ValueError):
    pass


def order_qty(qty: float, uom: str) -> float:
    """Round a shortfall up to something you can order."""
    return float(math.ceil(qty - 1e-9)) if uom.upper() in WHOLE_UNITS else math.ceil(qty * 10 - 1e-9) / 10


def _next_number(db: Session) -> str:
    n = (db.scalar(select(func.max(PurchaseRequest.id))) or 0) + 1
    return f"PR{n:05d}"


def availability(db: Session) -> list[dict]:
    """Per raw material: what's on hand, what open jobs still need (allocated),
    what's free, what's on order, what new demand needs, and the shortfall."""
    plan = {l["item_id"]: l for l in build_plan(db)["lines"]}
    allocated = defaultdict(float)
    for job in db.scalars(select(Job).where(Job.status.in_(OPEN_JOB_STATUSES))):
        for item_id, q in job_remaining_components(job).items():
            allocated[item_id] += q
    on_order = defaultdict(float)
    for pr in db.scalars(select(PurchaseRequest).where(PurchaseRequest.status.in_(OPEN_PR_STATUSES))):
        for ln in pr.lines:
            on_order[ln.item_id] += ln.qty
    rows = []
    for it in db.scalars(select(Item).where(Item.item_type == "raw").order_by(Item.no)):
        line = plan.get(it.id)
        gross = line["gross"] if line else 0.0
        shortage = line["net"] if line and line["action"] == "buy" else 0.0
        available = it.qty_on_hand - allocated[it.id]
        rows.append({
            "item_id": it.id, "item_no": it.no, "description": it.description, "uom": it.uom,
            "unit_cost": it.unit_cost, "vendor_no": it.vendor_no, "vendor_name": it.vendor_name,
            "lead_time_days": it.lead_time_days, "on_hand": it.qty_on_hand,
            "allocated": round(allocated[it.id], 4), "available": round(available, 4),
            "on_order": on_order[it.id], "planned_need": round(max(0.0, gross - allocated[it.id]), 4),
            "shortage": round(shortage, 4), "shortage_value": round(shortage * it.unit_cost, 2),
            "needed_by": line["earliest_due"] if line else None,
            "status": "short" if shortage > 1e-9 else ("tight" if available + on_order[it.id] < gross * 0.15 + 1e-9 and gross > 0 else "ok"),
        })
    return rows


def create_from_shortages(db: Session, operator: str = "") -> list[PurchaseRequest]:
    """One draft purchase request per vendor covering every shortage in the plan."""
    by_vendor: dict[str, list] = defaultdict(list)
    for line in build_plan(db)["lines"]:
        if line["action"] == "buy" and line["net"] > 1e-9:
            item = db.get(Item, line["item_id"])
            by_vendor[item.vendor_no].append((item, line))
    requests = []
    for vendor_no, rows in sorted(by_vendor.items()):
        pr = PurchaseRequest(number=_next_number(db), vendor_no=vendor_no,
                             vendor_name=rows[0][0].vendor_name or "Vendor not set", created_by=operator)
        for item, line in rows:
            needed = line["earliest_due"]
            # needed before the earliest job/demand date, allowing a day to stage material
            needed = (needed - timedelta(days=1)) if needed else date.today() + timedelta(days=item.lead_time_days)
            pr.lines.append(PurchaseRequestLine(item_id=item.id, item=item, qty=order_qty(line["net"], item.uom),
                                                unit_cost=item.unit_cost, needed_by=needed))
        pr.needed_by = min((ln.needed_by for ln in pr.lines if ln.needed_by), default=None)
        db.add(pr)
        db.flush()
        requests.append(pr)
    return requests


def update_line(db: Session, pr: PurchaseRequest, line_id: int, qty: float):
    if pr.status != "draft":
        raise PurchasingError("Only draft requests can be changed")
    line = next((l for l in pr.lines if l.id == line_id), None)
    if not line:
        raise PurchasingError("Line not found")
    if qty <= 0:
        pr.lines.remove(line)
    else:
        line.qty = qty


def send_to_bc(db: Session, pr: PurchaseRequest, bc) -> PurchaseRequest:
    if pr.status != "draft":
        raise PurchasingError(f"Request is {pr.status}; only drafts can be sent")
    if not pr.lines:
        raise PurchasingError("Request has no lines")
    if not pr.vendor_no:
        raise PurchasingError("Set a vendor for these items before sending")
    pr.bc_po_no = bc.create_purchase_order(
        pr.vendor_no, [BcPurchaseLine(item_no=l.item.no, qty=l.qty, unit_cost=l.unit_cost, needed_by=l.needed_by)
                       for l in pr.lines], reference=pr.number)
    pr.status, pr.sent_at = "sent", utcnow()
    return pr


def receive(db: Session, pr: PurchaseRequest, bc, heat_numbers: dict[int, str] | None = None) -> PurchaseRequest:
    """Simulated goods receipt (mock BC only). With real BC the receipt is posted
    in BC and arrives with the next stock pull; lots are recorded on Lots & trace."""
    if pr.status != "sent":
        raise PurchasingError("Only requests sent to BC can be received")
    if not hasattr(bc, "receive_purchase_order"):
        raise PurchasingError("Receive the purchase order in Business Central, then pull items & stock")
    bc.receive_purchase_order(pr.bc_po_no)
    for n, ln in enumerate(pr.lines, 1):
        ln.item.qty_on_hand += ln.qty
        if ln.item.lot_tracked:
            lot_no = f"{pr.bc_po_no}-{n}"
            if not db.scalar(select(Lot).where(Lot.item_id == ln.item_id, Lot.lot_no == lot_no)):
                db.add(Lot(item_id=ln.item_id, lot_no=lot_no, heat_no=(heat_numbers or {}).get(ln.id, ""),
                           supplier=pr.vendor_name, cert_ref="", qty_received=ln.qty, qty_on_hand=ln.qty))
    pr.status, pr.received_at = "received", utcnow()
    return pr


def cancel(pr: PurchaseRequest):
    if pr.status not in ("draft",):
        raise PurchasingError("Only drafts can be cancelled here; cancel a sent PO in BC")
    pr.status = "cancelled"
