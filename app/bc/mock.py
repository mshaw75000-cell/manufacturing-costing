"""Stand-in for Business Central used in development and demos.

Items are on Standard costing, as in the real company: every movement is valued
at the item's standard cost and journal lines never change it.

State lives in a JSON file so it survives restarts and you can see exactly
what would have been sent to BC.
"""
import json
import os
from dataclasses import asdict
from datetime import date, timedelta
from pathlib import Path

from .base import BcDemandLine, BcItem, BcJournalLine

DEFAULT_STATE = {
    "items": [
        {"no": "RM-STL-3MM", "description": "Steel sheet 3mm 2400x1200", "uom": "SHEET", "unit_cost": 185.00, "inventory": 40},
        {"no": "RM-TUBE-50", "description": "RHS tube 50x50x3 6.5m", "uom": "LEN", "unit_cost": 72.50, "inventory": 60},
        {"no": "RM-WIRE-MIG", "description": "MIG wire 0.9mm", "uom": "KG", "unit_cost": 9.80, "inventory": 30},
        {"no": "RM-PWD-BLK", "description": "Powder coat satin black", "uom": "KG", "unit_cost": 21.00, "inventory": 25},
        {"no": "RM-BOLT-M10", "description": "Bolt M10x30 zinc", "uom": "EA", "unit_cost": 0.45, "inventory": 2000},
        {"no": "SA-BRKT-01", "description": "Pressed bracket (sub-assembly)", "uom": "EA", "unit_cost": 15.50, "inventory": 0},
        {"no": "FG-FRAME-01", "description": "Welded frame, powder coated black", "uom": "EA", "unit_cost": 245.00, "inventory": 0},
    ],
    "demand": [
        {"item_no": "FG-FRAME-01", "qty": 24, "due_in_days": 10, "reference": "SO-1001/10000"},
    ],
    "journal": [],
}


class MockBusinessCentral:
    def __init__(self, path: str | None = None):
        self.path = Path(path or os.environ.get("BC_MOCK_FILE", "./bc_mock.json"))
        if not self.path.exists():
            self._save(DEFAULT_STATE)

    def _load(self) -> dict:
        return json.loads(self.path.read_text())

    def _save(self, state: dict):
        self.path.write_text(json.dumps(state, indent=2, default=str))

    def fetch_items(self) -> list[BcItem]:
        return [BcItem(**i) for i in self._load()["items"]]

    def fetch_demand(self) -> list[BcDemandLine]:
        return [
            BcDemandLine(item_no=d["item_no"], qty=d["qty"], reference=d["reference"],
                         due_date=date.today() + timedelta(days=d.get("due_in_days", 14)))
            for d in self._load()["demand"]
        ]

    def post_journal_line(self, line: BcJournalLine) -> dict:
        state = self._load()
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
        self._save(state)
        return entry
