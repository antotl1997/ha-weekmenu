"""Parse AH bonus mechanisms and compute savings per quantity.

Never guesses: anything that cannot be computed from the text (and the
product's own prices) is "unknown" and saves 0.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Literal

from app.ahclient import Product

Kind = Literal["price", "nth_discount", "x_plus_y", "n_for", "percent", "unknown"]

_NUM = r"(\d+(?:[.,]\d+)?)"


@dataclass(frozen=True)
class Deal:
    kind: Kind
    n: int = 1
    free: int = 0
    pct: float = 0
    total: float = 0


def _f(s: str) -> float:
    return float(s.replace(",", "."))


def parse_mechanism(text: str | None) -> Deal:
    if not text:
        return Deal("unknown")
    t = re.sub(r"\s+", " ", text.strip().lower())
    if m := re.fullmatch(r"(\d+)e halve prijs", t):
        return Deal("nth_discount", n=int(m[1]), pct=50)
    if m := re.fullmatch(r"(\d+)e gratis", t):
        return Deal("nth_discount", n=int(m[1]), pct=100)
    if m := re.fullmatch(r"(\d+) ?\+ ?(\d+) gratis", t):
        return Deal("x_plus_y", n=int(m[1]), free=int(m[2]))
    if m := re.fullmatch(rf"(\d+) voor {_NUM}", t):
        return Deal("n_for", n=int(m[1]), total=_f(m[2]))
    if m := re.fullmatch(rf"{_NUM} ?% korting", t):
        return Deal("percent", pct=_f(m[1]))
    if m := re.fullmatch(rf"voor {_NUM}", t):
        return Deal("price", total=_f(m[1]))
    return Deal("unknown")


def _effective(p: Product) -> Deal:
    d = parse_mechanism(p.mechanism)
    if d.kind == "unknown" and not p.mechanism and p.now < p.regular:
        return Deal("price")
    return d


def _group(d: Deal) -> int:
    if d.kind in ("nth_discount", "n_for"):
        return max(d.n, 1)
    if d.kind == "x_plus_y":
        return max(d.n + d.free, 1)
    return 1


def savings(p: Product, qty: int) -> float:
    d = _effective(p)
    if qty <= 0:
        return 0.0
    if d.kind == "price":
        s = max(p.regular - p.now, 0) * qty if p.now < p.regular else 0.0
    elif d.kind == "percent":
        s = p.regular * d.pct / 100 * qty
    elif d.kind == "nth_discount":
        s = (qty // d.n) * p.regular * d.pct / 100
    elif d.kind == "x_plus_y":
        s = (qty // (d.n + d.free)) * d.free * p.regular
    elif d.kind == "n_for":
        s = (qty // d.n) * max(d.n * p.regular - d.total, 0)
    else:
        s = 0.0
    return round(max(s, 0.0), 2)


def qty_for_deal(p: Product, need: int) -> int:
    d = _effective(p)
    g = _group(d)
    if need <= 0 or g <= 1:
        return need
    return math.ceil(need / g) * g
