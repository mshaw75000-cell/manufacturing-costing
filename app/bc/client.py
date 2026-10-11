"""Live Business Central client (BC online, API v2.0 + OData web services).

Reads:
  * items        -> standard API v2.0  /companies({id})/items
  * demand       -> standard API v2.0  /companies({id})/salesOrders?$expand=salesOrderLines
Writes:
  * purchase orders -> standard API v2.0 purchaseOrders + purchaseOrderLines
  * item journal lines -> OData web service for page 40 "Item Journal",
    published in BC (Web Services page) under the name in BC_ITEM_JOURNAL_SERVICE.
    Lines land in the configured journal batch; posting the batch is done in
    BC (manually, by job queue, or by a small AL extension). See README.
    Items are on Standard costing, so no unit amount is sent: BC values both
    consumption and output at the item's standard cost.
    Lot numbers are NOT sent: lot-tracked items in BC need item tracking lines,
    which page 40 can't create (see README, "Lots in BC").

Auth: Azure AD (Entra) app registration, OAuth2 client-credentials flow.
"""
import os
import time

import httpx

from .base import BcDemandLine, BcItem, BcJournalLine, BcPurchaseLine


class LiveBusinessCentral:
    def __init__(self):
        env = os.environ
        self.tenant = env["BC_TENANT_ID"]
        self.client_id = env["BC_CLIENT_ID"]
        self.client_secret = env["BC_CLIENT_SECRET"]
        self.environment = env.get("BC_ENVIRONMENT", "Production")
        self.company_id = env["BC_COMPANY_ID"]
        self.company_name = env["BC_COMPANY_NAME"]
        self.journal_service = env.get("BC_ITEM_JOURNAL_SERVICE", "ItemJournal")
        self.journal_template = env.get("BC_ITEM_JOURNAL_TEMPLATE", "ITEM")
        self.journal_batch = env.get("BC_ITEM_JOURNAL_BATCH", "MFGCOST")
        self.location_code = env.get("BC_LOCATION_CODE", "")
        base = f"https://api.businesscentral.dynamics.com/v2.0/{self.tenant}/{self.environment}"
        self.api_base = f"{base}/api/v2.0/companies({self.company_id})"
        self.odata_base = f"{base}/ODataV4/Company('{self.company_name}')"
        self._token = None
        self._token_expiry = 0.0
        self._http = httpx.Client(timeout=30)

    def _headers(self) -> dict:
        if not self._token or time.time() > self._token_expiry - 60:
            r = self._http.post(
                f"https://login.microsoftonline.com/{self.tenant}/oauth2/v2.0/token",
                data={"grant_type": "client_credentials", "client_id": self.client_id,
                      "client_secret": self.client_secret,
                      "scope": "https://api.businesscentral.dynamics.com/.default"})
            r.raise_for_status()
            body = r.json()
            self._token = body["access_token"]
            self._token_expiry = time.time() + int(body.get("expires_in", 3600))
        return {"Authorization": f"Bearer {self._token}", "Accept": "application/json"}

    def _get_all(self, url: str, params: dict | None = None) -> list[dict]:
        rows = []
        while url:
            r = self._http.get(url, headers=self._headers(), params=params)
            r.raise_for_status()
            body = r.json()
            rows.extend(body.get("value", []))
            url, params = body.get("@odata.nextLink"), None
        return rows

    def fetch_items(self) -> list[BcItem]:
        rows = self._get_all(f"{self.api_base}/items",
                             {"$select": "number,displayName,baseUnitOfMeasureCode,unitCost,inventory"})
        return [BcItem(no=r["number"], description=r.get("displayName", ""),
                       uom=r.get("baseUnitOfMeasureCode") or "EA",
                       unit_cost=float(r.get("unitCost") or 0), inventory=float(r.get("inventory") or 0))
                for r in rows]

    def fetch_demand(self) -> list[BcDemandLine]:
        orders = self._get_all(f"{self.api_base}/salesOrders",
                               {"$filter": "status eq 'Open' or status eq 'Released'",
                                "$expand": "salesOrderLines"})
        lines = []
        for so in orders:
            for ln in so.get("salesOrderLines", []):
                if ln.get("lineType") != "Item":
                    continue
                outstanding = float(ln.get("quantity") or 0) - float(ln.get("shippedQuantity") or 0)
                if outstanding <= 0:
                    continue
                lines.append(BcDemandLine(
                    item_no=ln["lineObjectNumber"], qty=outstanding,
                    due_date=ln.get("shipmentDate") or so.get("requestedDeliveryDate"),
                    reference=f"{so['number']}/{ln.get('sequence')}",
                    customer=so.get("customerName", "")))
        return lines

    def post_journal_line(self, line: BcJournalLine) -> dict:
        payload = {
            "Journal_Template_Name": self.journal_template,
            "Journal_Batch_Name": self.journal_batch,
            "Posting_Date": line.posting_date.isoformat(),
            "Entry_Type": "Negative Adjmt." if line.kind == "consumption" else "Positive Adjmt.",
            "Document_No": line.document_no,
            "Item_No": line.item_no,
            "Quantity": line.qty,
        }
        if self.location_code:
            payload["Location_Code"] = self.location_code
        r = self._http.post(f"{self.odata_base}/{self.journal_service}",
                            headers={**self._headers(), "Content-Type": "application/json"}, json=payload)
        r.raise_for_status()
        return r.json()

    def create_purchase_order(self, vendor_no: str, lines: list[BcPurchaseLine], reference: str) -> str:
        """Create a purchase order with one line per item. Returns the BC PO number."""
        headers = {**self._headers(), "Content-Type": "application/json"}
        dates = [ln.needed_by for ln in lines if ln.needed_by]
        header = {"vendorNumber": vendor_no}
        if dates:
            header["requestedReceiptDate"] = min(dates).isoformat()
        r = self._http.post(f"{self.api_base}/purchaseOrders", headers=headers, json=header)
        r.raise_for_status()
        po = r.json()
        for ln in lines:
            body = {"lineType": "Item", "lineObjectNumber": ln.item_no, "quantity": ln.qty,
                    "directUnitCost": ln.unit_cost}
            if ln.needed_by:
                body["expectedReceiptDate"] = ln.needed_by.isoformat()
            lr = self._http.post(f"{self.api_base}/purchaseOrders({po['id']})/purchaseOrderLines",
                                 headers=headers, json=body)
            lr.raise_for_status()
        return po["number"]
