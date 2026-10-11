import json
import os
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import BcPosting, Demand, Item, utcnow
from .base import BcJournalLine, BusinessCentral

_client: BusinessCentral | None = None


def get_bc() -> BusinessCentral:
    global _client
    if _client is None:
        if os.environ.get("BC_MODE", "mock") == "live":
            from .client import LiveBusinessCentral
            _client = LiveBusinessCentral()
        else:
            from .mock import MockBusinessCentral
            _client = MockBusinessCentral()
    return _client


def set_bc(client: BusinessCentral | None):
    global _client
    _client = client


def sync_items(db: Session, bc: BusinessCentral) -> dict:
    """Pull items, costs and inventory from BC.

    Inventory from BC is authoritative, except that movements still waiting in
    the outbox are re-applied so the local figure doesn't jump back.
    """
    pending = {}
    for p in db.scalars(select(BcPosting).where(BcPosting.status != "posted")):
        pending[p.item_no] = pending.get(p.item_no, 0.0) + (-p.qty if p.kind == "consumption" else p.qty)
    created = updated = 0
    now = utcnow()
    for bi in bc.fetch_items():
        item = db.scalar(select(Item).where(Item.no == bi.no))
        if not item:
            item = Item(no=bi.no, item_type=_guess_type(bi.no))
            db.add(item)
            created += 1
        else:
            updated += 1
        item.description = bi.description
        item.uom = bi.uom
        if bi.unit_cost or not item.unit_cost:
            item.unit_cost = bi.unit_cost
        item.qty_on_hand = bi.inventory + pending.get(bi.no, 0.0)
        item.bc_synced_at = now
    return {"created": created, "updated": updated}


def _guess_type(no: str) -> str:
    prefix = no.upper()[:3]
    return {"FG-": "finished", "SA-": "sub"}.get(prefix, "raw")


def sync_demand(db: Session, bc: BusinessCentral) -> dict:
    """Pull open sales demand from BC; upsert by reference."""
    created = updated = skipped = 0
    seen = set()
    for line in bc.fetch_demand():
        item = db.scalar(select(Item).where(Item.no == line.item_no))
        if not item:
            skipped += 1
            continue
        seen.add(line.reference)
        d = db.scalar(select(Demand).where(Demand.source == "bc", Demand.reference == line.reference))
        due = date.fromisoformat(line.due_date) if isinstance(line.due_date, str) else line.due_date
        if d:
            if d.status == "open":
                d.qty, d.due_date, d.item_id = line.qty, due, item.id
                updated += 1
        else:
            db.add(Demand(item_id=item.id, qty=line.qty, due_date=due, source="bc", reference=line.reference))
            created += 1
    # Lines that disappeared from BC (shipped / deleted) are no longer demand.
    closed = 0
    for d in db.scalars(select(Demand).where(Demand.source == "bc", Demand.status == "open")):
        if d.reference not in seen:
            d.status = "cancelled"
            closed += 1
    return {"created": created, "updated": updated, "closed": closed, "skipped_unknown_items": skipped}


def process_outbox(db: Session, bc: BusinessCentral, limit: int = 200) -> dict:
    rows = db.scalars(select(BcPosting).where(BcPosting.status.in_(("pending", "failed")))
                      .order_by(BcPosting.id).limit(limit)).all()
    posted = failed = 0
    for p in rows:
        p.attempts += 1
        try:
            response = bc.post_journal_line(BcJournalLine(
                kind=p.kind, document_no=p.document_no, item_no=p.item_no,
                qty=p.qty, unit_cost=p.unit_cost, posting_date=date.today()))
            p.response = json.loads(json.dumps(response, default=str))
            p.status, p.error, p.posted_at = "posted", "", utcnow()
            posted += 1
        except Exception as e:  # keep going; failed rows are retried next run
            p.status, p.error = "failed", str(e)[:2000]
            failed += 1
        db.commit()
    return {"posted": posted, "failed": failed}
