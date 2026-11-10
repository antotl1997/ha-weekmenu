"""Vaste boodschappen in de bonus: what the household often buys besides dinner.

A product is suggested when it was bought at least MIN_TIMES times (orders and till
receipts together) in the WINDOW_DAYS days before today, is in this week's bonus, is
not already in the menu and is not a dinner product (meat, fish, vegetables, potatoes,
meals: the menu is about those; sandwich meats, "Vleeswaren", do stay). The quantity is the usual one (median per
purchase, rounded up) topped up so the deal applies. Only a suggestion: nothing is
written until the user taps it in the app.
"""

from __future__ import annotations

import dataclasses
import math
import statistics
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Literal

from app.ahclient import Product
from app.deals import qty_for_deal, savings

WINDOW_DAYS = 61
MIN_TIMES = 2
# substrings of the AH main category, case-insensitive ("Vlees", "Vis",
# "Groente, aardappelen", "Maaltijden, salades"); "Vleeswaren" is beleg and stays
DINNER_CATEGORIES = ("vlees", "vis", "kip", "groente", "aardappel", "maaltijden")
NOT_DINNER = ("vleeswaren",)


@dataclass
class Purchase:
    product_id: int  # webshop id; 0 when only the hq id is known (a till receipt line)
    title: str
    qty: int
    day: date
    source: Literal["online", "winkel"]
    hq_id: int | None = None  # AH article number: the id on a till receipt


@dataclass
class Staple:
    product: Product
    times: int
    usual_qty: int
    qty: int
    savings: float

    def to_dict(self) -> dict:
        return {"product": dataclasses.asdict(self.product), "times": self.times,
                "usual_qty": self.usual_qty, "qty": self.qty, "savings": self.savings}

    @classmethod
    def from_dict(cls, d: dict) -> "Staple":
        return cls(Product(**d["product"]), int(d["times"]), int(d["usual_qty"]),
                   int(d["qty"]), float(d["savings"]))


def is_dinner(p: Product) -> bool:
    cat = (p.category or "").lower()
    if any(w in cat for w in NOT_DINNER):
        return False
    return any(w in cat for w in DINNER_CATEGORIES)


def _matches(buy: Purchase, p: Product) -> bool:
    if buy.product_id and buy.product_id == p.id:
        return True
    return bool(buy.hq_id and p.hq_id and buy.hq_id == p.hq_id)


def suggest_staples(
    purchases: list[Purchase], bonus: list[Product], menu_basket: dict[int, int], today: date
) -> list[Staple]:
    since = today - timedelta(days=WINDOW_DAYS)
    recent = [b for b in purchases if since <= b.day <= today and b.qty > 0]
    out: list[Staple] = []
    seen: set[int] = set()
    for p in bonus:
        if p.id in seen or p.id in menu_basket or not p.orderable or is_dinner(p):
            continue
        seen.add(p.id)
        bought = [b.qty for b in recent if _matches(b, p)]
        if len(bought) < MIN_TIMES:
            continue
        usual = max(1, math.ceil(statistics.median(bought)))
        qty = qty_for_deal(p, usual)
        saved = savings(p, qty)
        if saved <= 0:
            continue
        out.append(Staple(p, len(bought), usual, qty, saved))
    out.sort(key=lambda s: (-s.savings, s.product.title))
    return out


def staple_list(log) -> list[Staple]:
    return [Staple.from_dict(x) for x in log.staples]


def extras_savings(log) -> float:
    """The saving of the staples that were added this week (WeekLog.extras_added)."""
    return round(sum(s.savings for s in staple_list(log) if s.product.id in log.extras_added), 2)


def merge(log, new: list[Staple]) -> None:
    """Replace the suggestions; staples already added (or in flight) stay listed."""
    keep = [s for s in staple_list(log)
            if (s.product.id in log.extras_added or s.product.id in log.pending)
            and s.product.id not in {n.product.id for n in new}]
    log.staples = [s.to_dict() for s in new + keep]
    log.extras_savings = extras_savings(log)


def open_count(log, menu_basket: dict[int, int] | None = None) -> int:
    """Suggestions not added yet (as a staple or a loose offer) and not in the menu:
    the number in the message."""
    menu = menu_basket or {}
    return sum(1 for s in staple_list(log)
               if s.product.id not in log.extras_added and s.product.id not in log.offers_added
               and s.product.id not in log.pending and s.product.id not in menu)
