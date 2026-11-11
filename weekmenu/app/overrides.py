"""v0.9 "Klopt niet": what the user decided per ingredient, for good (Anto, answer 6).

`/data/overrides.json` maps the normalised ingredient name (`override_key`: no amount or
unit in front, folded, words only) to one choice:

- "product": always this AH product (kept whole, so no AH call is needed to use it);
- "skip": "Hoeft niet", never buy anything for it;
- "pantry": "Hebben we al", counts as in the house.

The choice goes before the heuristics in `choose_product` and `is_pantry`, in every week,
until it is taken back in Instellingen ("Mijn correcties"). Written with the schema and
`durable.write_json` like every other file; a file from a newer add-on is read but never
written (SchemaTooNew). Callers write under `ctx.lock`.
"""

from __future__ import annotations

import dataclasses
import json
import logging
from datetime import datetime
from pathlib import Path

from app.ahclient import Product
from app.durable import too_new, write_json
from app.ingredients import KINDS, PRODUCT, override_key

log = logging.getLogger(__name__)

OVERRIDES_SCHEMA = 1

__all__ = ["OVERRIDES_SCHEMA", "Overrides", "override_key", "make_entry"]


def _valid(entry) -> bool:
    if not isinstance(entry, dict) or entry.get("kind") not in KINDS:
        return False
    if entry["kind"] == PRODUCT:
        p = entry.get("product")
        if not isinstance(p, dict):
            return False
        try:
            Product(**p)
        except TypeError:
            return False
    return True


def make_entry(kind: str, text: str, product: Product | None, at: datetime) -> dict:
    """One choice: the ingredient text it was made on (for the list), and when."""
    entry = {"kind": kind, "text": text, "at": at.isoformat(timespec="minutes")}
    if kind == PRODUCT and product is not None:
        entry["product"] = dataclasses.asdict(product)
    return entry


class Overrides:
    def __init__(self, path: Path):
        self.path = Path(path)

    def load(self) -> dict[str, dict]:
        """key -> choice. A missing or unreadable file is no choices (logged)."""
        try:
            raw = self.path.read_bytes()
        except FileNotFoundError:
            return {}
        except OSError as e:
            log.warning("%s niet te lezen (%s)", self.path.name, type(e).__name__)
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except ValueError:
            log.warning("%s is beschadigd: correcties tellen niet mee", self.path.name)
            return {}
        items = data.get("items") if isinstance(data, dict) else None
        if not isinstance(items, dict):
            return {}
        return {k: v for k, v in items.items() if isinstance(k, str) and k and _valid(v)}

    def too_new(self) -> bool:
        return too_new(self.path, OVERRIDES_SCHEMA)

    def put(self, key: str, entry: dict) -> None:
        """Set one choice (SchemaTooNew from a newer add-on's file: nothing written)."""
        items = self.load()
        items[key] = entry
        write_json(self.path, {"items": items}, OVERRIDES_SCHEMA)

    def remove(self, key: str) -> bool:
        """Take one choice back; False when there was none."""
        items = self.load()
        if key not in items:
            return False
        del items[key]
        write_json(self.path, {"items": items}, OVERRIDES_SCHEMA)
        return True

    def listing(self) -> list[dict]:
        """For "Mijn correcties": newest first."""
        out = []
        for key, e in self.load().items():
            p = e.get("product") if e["kind"] == PRODUCT else None
            out.append({"key": key, "text": e.get("text") or key, "kind": e["kind"],
                        "product": p.get("title") if p else None, "at": e.get("at")})
        out.sort(key=lambda x: (x["at"] or "", x["key"]), reverse=True)
        return out
