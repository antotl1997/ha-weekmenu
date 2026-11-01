"""Anonymize raw recordings: python -m tools.anonymize [history]

Reads %APPDATA%\\weekmenu-test\\raw\\*.json, writes tests/fixtures/*.json.
With `history`: only raw-history\\*.json (tools.record_history), written as
tests/fixtures/history-*.json.
Product data and prices are kept; personal fields are dropped.
"""
import json
import os
import re
from pathlib import Path

RAW = Path(os.environ.get("APPDATA", ".")) / "weekmenu-test" / "raw"
RAW_HISTORY = RAW.parent / "raw-history"
OUT = Path(__file__).resolve().parent.parent / "tests" / "fixtures"

FORBIDDEN_KEY = re.compile(
    r"token|email|address|street|memberid|customerid|phone|firstname|lastname"
    r"|initials|housenumber|postalcode|zip"
    r"|city|invoice|payment|cashier|terminal|transaction|cardnumber|storeaddress"
    r"|cancellationreason|closingtime|deliverytimeperiod|timedisplay",
    re.I,
)
EMAIL = re.compile(r"[^\s@]+@[^\s@]+")
POSTCODE = re.compile(r"\b\d{4}\s?[A-Z]{2}\b")
FIXED_DATE = "2026-01-07"
FIXED_ISO = "2026-01-07T12:00:00"
FIXED_ID = 123456


def scrub_string(s: str) -> str:
    s = EMAIL.sub("redacted", s)
    return POSTCODE.sub("redacted", s)


def walk(o, in_order: bool):
    if isinstance(o, dict):
        out = {}
        for k, v in o.items():
            if FORBIDDEN_KEY.search(k):
                continue
            kl = k.lower()
            if in_order and kl in ("id", "orderid") :
                out[k] = FIXED_ID
            elif in_order and re.search(r"date|time|slot|deliver", kl) and not isinstance(
                v, (dict, list)
            ):
                out[k] = FIXED_ISO if isinstance(v, str) else 1767787200000
            else:
                out[k] = walk(v, in_order)
        return out
    if isinstance(o, list):
        return [walk(v, in_order) for v in o]
    if isinstance(o, str):
        return scrub_string(o)
    return o


def anonymize(name: str, data):
    in_order = name.startswith("order-")
    data = walk(data, in_order)
    if name.startswith("shoppinglist__") and isinstance(data.get("body"), dict):
        b = data["body"]
        b["id"] = "00000000-0000-0000-0000-000000000000"
        b["dateLastSynced"] = FIXED_ISO
        b["dateLastSyncedMillis"] = 1767787200000
    return data


# --- purchase history (orders and receipts) ---

FAKE_ORDER_BASE = 900001
NOON = "T12:00:00.000Z"
HEALTH = ("gezondheid", "medicijn", "geneesmiddel")


class HistoryIds:
    """Real order/receipt ids -> stable fake ones, consistent across all files."""

    def __init__(self):
        self.orders: dict = {}
        self.receipts: dict = {}

    def order(self, real) -> int:
        return self.orders.setdefault(real, FAKE_ORDER_BASE + len(self.orders))

    def receipt(self, real) -> str:
        return self.receipts.setdefault(real, f"receipt-{len(self.receipts) + 1}")


def _body(data):
    return data.get("body") if isinstance(data, dict) and "status" in data else data


def _gql_data(data) -> dict:
    body = _body(data)
    return (body.get("data") or {}) if isinstance(body, dict) else {}


def anonymize_history(name: str, data, ids: HistoryIds):
    """Strip order/receipt ids, payments, address, time slots and store/till data.

    Kept: product ids (webshop and hq), titles, quantities, prices, dates.
    """
    body = _body(data)
    if name.startswith("graphql-history-fulfillments"):
        for f in ((_gql_data(data).get("orderFulfillments") or {}).get("result")) or []:
            f["orderId"] = ids.order(f.get("orderId"))
            if f.get("statusDescription") != "Geannuleerd":
                f["statusDescription"] = "redacted"  # may tell a payment status
            slot = (f.get("delivery") or {}).get("slot") or {}
            slot.pop("timeDisplay", None)
    elif name.startswith("history-order-details") and isinstance(body, dict):
        if "orderId" in body:
            body["orderId"] = ids.order(body["orderId"])
        for k in ("address", "invoiceId", "orderPayments", "closingTime",
                  "deliveryTimePeriod", "cancellationReason"):
            body.pop(k, None)
        for g in body.get("groupedProductsInTaxonomy") or []:
            # health products (medicine) are special-category data: never in a fixture
            g["orderedProducts"] = [
                op for op in g.get("orderedProducts") or []
                if not any(w in str((op.get("product") or {}).get(c) or "").lower()
                           for w in HEALTH for c in ("mainCategory", "subCategory"))
            ]
        body["groupedProductsInTaxonomy"] = [
            g for g in body.get("groupedProductsInTaxonomy") or [] if g["orderedProducts"]
        ]
    elif name.startswith("graphql-history-receipts"):
        for r in ((_gql_data(data).get("posReceiptsPage") or {}).get("posReceipts")) or []:
            r["id"] = ids.receipt(r.get("id"))
            if isinstance(r.get("dateTime"), str):
                r["dateTime"] = r["dateTime"][:10] + NOON  # keep the date, drop the time
    elif name.startswith("graphql-history-receipt"):
        d = _gql_data(data).get("posReceiptDetails")
        if isinstance(d, dict):
            if "id" in d:
                d["id"] = ids.receipt(d["id"])
            d.pop("payments", None)
    return walk(data, False)


def out_name(stem: str) -> str:
    """Every history fixture starts with `history-`."""
    if stem.startswith("graphql-history-"):
        return "history-graphql-" + stem[len("graphql-history-"):]
    return stem if stem.startswith("history-") else "history-" + stem


def main(history_only: bool = False):
    OUT.mkdir(parents=True, exist_ok=True)
    for f in [] if history_only else sorted(RAW.glob("*.json")):
        data = json.loads(f.read_text(encoding="utf-8"))
        clean = anonymize(f.stem, data)
        (OUT / f.name).write_text(
            json.dumps(clean, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
        )
    ids = HistoryIds()
    # the lists first, so the detail files reuse their fake ids in list order
    files = sorted(
        RAW_HISTORY.glob("*.json") if RAW_HISTORY.is_dir() else [],
        key=lambda f: (0 if ("fulfillments" in f.stem or "receipts__" in f.stem) else 1,
                       f.stem),
    )
    for f in files:
        data = json.loads(f.read_text(encoding="utf-8"))
        clean = anonymize_history(f.stem, data, ids)
        (OUT / f"{out_name(f.stem)}.json").write_text(
            json.dumps(clean, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
        )
    print("written", len(list(OUT.glob("*.json"))), "fixtures to", OUT)


if __name__ == "__main__":
    import sys

    main(history_only="history" in sys.argv[1:])
