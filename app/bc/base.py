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


@dataclass
class BcDemandLine:
    item_no: str
    qty: float
    due_date: date | None
    reference: str  # e.g. "SO-1001/10000" (sales order / line)


@dataclass
class BcJournalLine:
    kind: str  # consumption -> Negative Adjmt., output -> Positive Adjmt.
    document_no: str
    item_no: str
    qty: float
    unit_cost: float
    posting_date: date


class BusinessCentral(Protocol):
    def fetch_items(self) -> list[BcItem]: ...
    def fetch_demand(self) -> list[BcDemandLine]: ...
    def post_journal_line(self, line: BcJournalLine) -> dict: ...
