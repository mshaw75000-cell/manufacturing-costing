from dataclasses import dataclass
from datetime import date
from typing import Protocol


@dataclass
class BcItem:
    no: str
    description: str
    uom: str
    unit_cost: float
    inventory: float
    vendor_no: str = ""
    vendor_name: str = ""
    lead_time_days: int = 7


@dataclass
class BcDemandLine:
    item_no: str
    qty: float
    due_date: date | None
    reference: str  # e.g. "SO-1001/10000" (sales order / line)
    customer: str = ""


@dataclass
class BcJournalLine:
    kind: str  # consumption -> Negative Adjmt., output -> Positive Adjmt.
    document_no: str
    item_no: str
    qty: float
    unit_cost: float
    posting_date: date
    lot_no: str = ""


@dataclass
class BcPurchaseLine:
    item_no: str
    qty: float
    unit_cost: float
    needed_by: date | None


class BusinessCentral(Protocol):
    def fetch_items(self) -> list[BcItem]: ...
    def fetch_demand(self) -> list[BcDemandLine]: ...
    def post_journal_line(self, line: BcJournalLine) -> dict: ...
    def create_purchase_order(self, vendor_no: str, lines: list[BcPurchaseLine], reference: str) -> str: ...
