"""Stand-in for Business Central used in development and demos.

Items are on Standard costing, as in the real company: every movement is valued
at the item's standard cost and journal lines never change it.

State lives in a JSON file so it survives restarts and you can see exactly
what would have been sent to BC. To make demos feel live, the state can hold
an `incoming` queue of sales orders: each demand pull releases the next ones,
as if customers had just placed them.
"""
import json
import os
from dataclasses import asdict
from datetime import date, timedelta
from pathlib import Path

from .base import BcDemandLine, BcItem, BcJournalLine, BcPurchaseLine

DEFAULT_STATE = {
    "items": [
        {"no": "RM-STL-3MM", "description": "Steel sheet 3mm 2400x1200", "uom": "SHEET", "unit_cost": 185.00, "inventory": 40,
         "vendor_no": "V-1001", "vendor_name": "BlueScope Distribution", "lead_time_days": 5},
        {"no": "RM-TUBE-50", "description": "RHS tube 50x50x3 6.5m", "uom": "LEN", "unit_cost": 72.50, "inventory": 60,
         "vendor_no": "V-1002", "vendor_name": "Orrcon Steel", "lead_time_days": 7},
        {"no": "RM-WIRE-MIG", "description": "MIG wire 0.9mm", "uom": "KG", "unit_cost": 9.80, "inventory": 30,
         "vendor_no": "V-1004", "vendor_name": "BOC Gas & Gear", "lead_time_days": 3},
        {"no": "RM-PWD-BLK", "description": "Powder coat satin black", "uom": "KG", "unit_cost": 21.00, "inventory": 25,
         "vendor_no": "V-1003", "vendor_name": "Dulux Powders", "lead_time_days": 10},
        {"no": "RM-BOLT-M10", "description": "Bolt M10x30 zinc", "uom": "EA", "unit_cost": 0.45, "inventory": 2000,
         "vendor_no": "V-1005", "vendor_name": "Bolt & Nut Supplies", "lead_time_days": 4},
        {"no": "SA-BRKT-01", "description": "Pressed bracket (sub-assembly)", "uom": "EA", "unit_cost": 15.50, "inventory": 0},
        {"no": "FG-FRAME-01", "description": "Welded frame, powder coated black", "uom": "EA", "unit_cost": 245.00, "inventory": 0},
    ],
    "demand": [
        {"item_no": "FG-FRAME-01", "qty": 24, "due_in_days": 10, "reference": "SO-1001/10000", "customer": "Coastal Fitouts"},
    ],
    "incoming": [],
    "journal": [],
    "purchase_orders": [],
}


class MockBusinessCentral:
    def __init__(self, path: str | None = None):
        self.path = Path(path or os.environ.get("BC_MOCK_FILE", "./bc_mock.json"))
        if not self.path.exists():
            self.save_state(DEFAULT_STATE)

    def load_state(self) -> dict:
        state = json.loads(self.path.read_text())
        for k in ("incoming", "journal", "purchase_orders"):
            state.setdefault(k, [])
        return state

    def save_state(self, state: dict):
        self.path.write_text(json.dumps(state, indent=2, default=str))

    def fetch_items(self) -> list[BcItem]:
        return [BcItem(**i) for i in self.load_state()["items"]]

    def fetch_demand(self) -> list[BcDemandLine]:
        state = self.load_state()
        # New customer orders arrive with each pull (two at a time).
        for _ in range(min(2, len(state["incoming"]))):
            order = state["incoming"].pop(0)
            if "due_in_days" in order:
                order["due_date"] = (date.today() + timedelta(days=order.pop("due_in_days"))).isoformat()
            state["demand"].append(order)
        self.save_state(state)
        lines = []
        for d in state["demand"]:
            due = d.get("due_date") or (date.today() + timedelta(days=d.get("due_in_days", 14))).isoformat()
            lines.append(BcDemandLine(item_no=d["item_no"], qty=d["qty"], reference=d["reference"],
                                      due_date=date.fromisoformat(due), customer=d.get("customer", "")))
        return lines

    def post_journal_line(self, line: BcJournalLine) -> dict:
        state = self.load_state()
        sign = -1 if line.kind == "consumption" else 1
        for item in state["items"]:
            if item["no"] == line.item_no:
                item["inventory"] += sign * line.qty
                break
        else:
            raise ValueError(f"Item {line.item_no} not found in BC")
        entry = {"entry_no": len(state["journal"]) + 1, **asdict(line),
                 "value": round(sign * line.qty * item["unit_cost"], 4)}
        state["journal"].append(entry)
        self.save_state(state)
        return entry

    def create_purchase_order(self, vendor_no: str, lines: list[BcPurchaseLine], reference: str) -> str:
        state = self.load_state()
        known = {i["no"] for i in state["items"]}
        for ln in lines:
            if ln.item_no not in known:
                raise ValueError(f"Item {ln.item_no} not found in BC")
        po_no = f"PO-{10001 + len(state['purchase_orders'])}"
        state["purchase_orders"].append({
            "no": po_no, "vendor_no": vendor_no, "reference": reference, "status": "Released",
            "lines": [asdict(ln) for ln in lines]})
        self.save_state(state)
        return po_no

    def receive_purchase_order(self, po_no: str) -> None:
        """Demo only: in real BC the warehouse posts the receipt."""
        state = self.load_state()
        po = next((p for p in state["purchase_orders"] if p["no"] == po_no), None)
        if not po:
            raise ValueError(f"Purchase order {po_no} not found in BC")
        if po["status"] == "Received":
            raise ValueError(f"{po_no} is already received")
        items = {i["no"]: i for i in state["items"]}
        for ln in po["lines"]:
            items[ln["item_no"]]["inventory"] += ln["qty"]
        po["status"] = "Received"
        self.save_state(state)
