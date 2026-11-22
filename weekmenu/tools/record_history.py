"""Record raw purchase-history responses outside the repo: python -m tools.record_history

Read-only: GET requests and GraphQL queries only (plus the token refresh that
AhClient does on its own). Writes to %APPDATA%\\weekmenu-test\\raw-history\\.
Request headers are never written; nothing but status and file name is printed.
Stops at once on HTTP 401, 403 or 429, and after MAX_CALLS calls.
"""
import asyncio
import os
from pathlib import Path

import httpx

from app.ahclient import AhClient
from app.tokens import TokenStore
from tools import record_fixtures as rf

ROOT = Path(os.environ["APPDATA"]) / "weekmenu-test"
RAW = ROOT / "raw-history"
MAX_CALLS = 40
DETAILS = 5

Q_FULFILLMENTS = (
    "query Fulfillments {\n  orderFulfillments(status: CLOSED) {\n    result {\n"
    "      orderId statusDescription shoppingType modifiable\n"
    "      totalPrice { totalPrice { amount } }\n"
    "      delivery { slot { date dateDisplay timeDisplay } }\n    }\n  }\n}"
)
Q_RECEIPTS = (
    "query Receipts($limit: Int!) {\n"
    "  posReceiptsPage(pagination: {offset: 0, limit: $limit}) {\n"
    "    posReceipts { id dateTime totalAmount { amount } }\n  }\n}"
)
RECEIPT_PRODUCT = "name quantity price { amount } amount { amount }"
Q_RECEIPT = (
    "query Receipt($id: String!) {\n  posReceiptDetails(id: $id) {\n    id\n"
    "    products { %s }\n    discounts { name amount { amount } }\n"
    "    payments { method amount { amount } }\n  }\n}"
)
# Introspection is disabled: unknown fields come back as validation errors that
# name the type and often suggest the real field ("Did you mean ...?").
PROBE_FIELDS = "id productId webshopId wsId hqId gtin ean barcode articleNumber sku product"


class Stop(Exception):
    pass


class HistoryRecorder(rf.Recorder):
    def save(self, name, data, status):
        RAW.mkdir(parents=True, exist_ok=True)
        rf.RAW = RAW  # Recorder.save writes to the module-level RAW
        super().save(name, data, status)

    async def fetch(self, method, path, json_body=None):
        if self.calls >= MAX_CALLS:
            raise Stop(f"call budget of {MAX_CALLS} reached")
        status, body = await super().fetch(method, path, json_body)
        if status in (401, 403, 429):
            self.save(f"STOPPED__http-{status}", {"status": status, "path": path}, status)
            raise Stop(f"HTTP {status} on {path}")
        return status, body


def gql_data(body) -> dict:
    return (body or {}).get("data") or {} if isinstance(body, dict) else {}


async def record(rec: HistoryRecorder) -> None:
    st, body = await rec.gql("history-fulfillments", "closed", Q_FULFILLMENTS, {}, wrap=True)
    result = ((gql_data(body).get("orderFulfillments") or {}).get("result")) or []
    order_ids = [f["orderId"] for f in result if isinstance(f, dict) and f.get("orderId")]
    for i, oid in enumerate(order_ids[:DETAILS]):
        await rec.rest(
            "history-order-details", f"n{i}",
            f"/mobile-services/order/v1/{oid}/details-grouped-by-taxonomy", wrap=True,
        )

    st, body = await rec.gql("history-receipts", "limit50", Q_RECEIPTS, {"limit": 50}, wrap=True)
    receipts = ((gql_data(body).get("posReceiptsPage") or {}).get("posReceipts")) or []
    receipt_ids = [r["id"] for r in receipts if isinstance(r, dict) and r.get("id")]
    if receipt_ids:
        await rec.gql(
            "history-receipt-probe", "fields", Q_RECEIPT % PROBE_FIELDS,
            {"id": receipt_ids[0]}, wrap=True,
        )
    for i, rid in enumerate(receipt_ids[:DETAILS]):
        await rec.gql(
            "history-receipt", f"n{i}", Q_RECEIPT % RECEIPT_PRODUCT, {"id": rid}, wrap=True
        )

    st, meta = await rec.rest("history-bonus-metadata", "v3", "/mobile-services/bonuspage/v3/metadata")
    periods = (meta or {}).get("periods") or [] if isinstance(meta, dict) else []
    for idx, period in enumerate(periods[:2]):
        start = period["bonusStartDate"]
        await rec.rest(
            "history-previously-bought", f"p{idx}",
            "/mobile-services/bonuspage/v2/section/previously-bought"
            f"?application=AHWEBSHOP&date={start}",
            wrap=True,
        )


async def main():
    store = TokenStore(ROOT / "tokens.json")
    async with httpx.AsyncClient(timeout=30) as http:
        rec = HistoryRecorder(AhClient(store, http))
        try:
            await record(rec)
        except Stop as e:
            print("STOPPED:", e)
        print("total calls:", rec.calls)


if __name__ == "__main__":
    asyncio.run(main())
