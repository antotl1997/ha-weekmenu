"""Anonymize raw recordings: python -m tools.anonymize

Reads %APPDATA%\\weekmenu-test\\raw\\*.json, writes tests/fixtures/*.json.
Product data and prices are kept; personal fields are dropped.
"""
import json
import os
import re
from pathlib import Path

RAW = Path(os.environ.get("APPDATA", ".")) / "weekmenu-test" / "raw"
OUT = Path(__file__).resolve().parent.parent / "tests" / "fixtures"

FORBIDDEN_KEY = re.compile(
    r"token|email|address|street|memberid|customerid|phone|firstname|lastname"
    r"|initials|housenumber|postalcode|zip",
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


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    for f in sorted(RAW.glob("*.json")):
        data = json.loads(f.read_text(encoding="utf-8"))
        clean = anonymize(f.stem, data)
        (OUT / f.name).write_text(
            json.dumps(clean, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
        )
    print("written", len(list(OUT.glob("*.json"))), "fixtures to", OUT)


if __name__ == "__main__":
    main()
