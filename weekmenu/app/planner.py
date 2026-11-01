"""Choose the week's dinners around the AH bonus.

Candidates come from Allerhande searches for the best bonus products. Every candidate
is scored on the bonus euros it saves for 2 persons, then a menu is picked that keeps
the rules: at most 45 minutes active cooking on weekdays, at least three cuisines
(or as many as there are evenings), at most one fish evening, nothing from the last
four menu weeks. Too few candidates: fewer evenings, never looser rules.
"""

from __future__ import annotations

import dataclasses
import itertools
import logging
import math
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import yaml

from app.ahclient import AhApiError, AhLoginExpired, AhNotJson, Product, Recipe, RecipeSummary
from app.deals import parse_mechanism, qty_for_deal, savings
from app.ingredients import (
    choose_product,
    is_pantry,
    keywords,
    pack_fraction,
    packs_needed,
    parse_ingredient,
    parse_unit_size,
)
from app.week import is_weekend

MAX_COOK_WEEKDAY = 45
MIN_CUISINES = 3
MAX_FISH = 1
TOP_BONUS = 25
MAX_RECIPE_SEARCHES = 25
MAX_RECIPE_DETAILS = 40  # get_recipe calls per build_plan, the per-person scaling included
SCORE_SERVINGS = 2  # default persons when an evening has none
MAIN_COURSE = "hoofdgerecht"
# Backtracking budget for picking the menu (nodes over all attempts of one build).
# Spent: the best valid partial menu found so far is used and Plan.budget_exhausted set.
_SEARCH_BUDGET = 500_000  # about 1.2 s of searching on a desktop when it is all spent

log = logging.getLogger(__name__)

_CONF = yaml.safe_load((Path(__file__).parent / "keukens.yaml").read_text(encoding="utf-8"))


def _fold(text: str) -> str:
    t = unicodedata.normalize("NFKD", (text or "").lower())
    return "".join(c for c in t if not unicodedata.combining(c))


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z]+", _fold(text))


def _flist(key: str) -> list[str]:
    return [_fold(str(x)) for x in _CONF.get(key) or []]


CUISINES: list[dict] = [
    {
        "naam": k["naam"],
        "cuisines": [_fold(x) for x in k.get("cuisines") or []],
        "titel": [_fold(x) for x in k.get("titel") or []],
        "ingredienten": [_fold(x) for x in k.get("ingredienten") or []],
    }
    for k in _CONF["keukens"]
]
FISH_WORDS = _flist("vis")
FISH_EXCEPTIONS = _flist("vis_uitzonderingen")
CHICKEN_WORDS = _flist("kip")
CHICKEN_EXCEPTIONS = _flist("kip_uitzonderingen")
SHELF_STABLE = _flist("houdbaar")
EXCLUDE_TITLES = _flist("uitsluiten_titel")
BONUS_CATEGORIES = _flist("bonus_categorieen")
CORE_WORDS = sorted(_flist("kernwoorden"), key=len, reverse=True)
# Not a product: brand and size words in a bonus title.
_TITLE_NOISE = frozenset({"ah", "excellent", "biologisch", "biologische", "gram", "kilo",
                          "stuks", "stuk", "per", "pack", "liter", "baby", "iets", "eetrijp"})


# --- data --------------------------------------------------------------------------


@dataclass
class Settings:
    evenings: list[date]
    persons: dict[date, int]
    pantry: list[str]
    recent_recipe_ids: set[int]


@dataclass
class Line:
    product: Product
    qty: int  # whole packs for this dish alone
    from_ingredient: str
    fraction: float | None = None  # the amount as part of a pack; None: not convertible


@dataclass
class Dish:
    date: date
    recipe: Recipe
    cuisine: str
    is_fish: bool
    lines: list[Line]
    savings: float
    persons: int = SCORE_SERVINGS
    cook: int | None = None  # active minutes; the 45-minute limit applies to this only
    oven: int | None = None
    wait: int | None = None
    pantry: list[str] = field(default_factory=list)  # skipped pantry texts ("in huis")
    not_found: list[str] = field(default_factory=list)
    search_term: str = ""


@dataclass
class Plan:
    dishes: list[Dish]
    basket: dict[int, int]
    products: dict[int, Product]
    savings: float
    not_found: list[str]
    short_by: int
    pantry: list[str] = field(default_factory=list)  # the pantry list the plan was made with
    budget_exhausted: bool = False  # the menu search ran out of budget: maybe not the best

    def to_dict(self) -> dict:
        return {
            "dishes": [_dish_to_dict(d) for d in self.dishes],
            "basket": {str(k): v for k, v in self.basket.items()},
            "products": {str(k): dataclasses.asdict(p) for k, p in self.products.items()},
            "savings": self.savings,
            "not_found": list(self.not_found),
            "short_by": self.short_by,
            "pantry": list(self.pantry),
            "budget_exhausted": self.budget_exhausted,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Plan":
        return cls(
            dishes=[_dish_from_dict(x) for x in d["dishes"]],
            basket={int(k): int(v) for k, v in d["basket"].items()},
            products={int(k): Product(**p) for k, p in d["products"].items()},
            savings=float(d["savings"]),
            not_found=list(d["not_found"]),
            short_by=int(d["short_by"]),
            pantry=list(d.get("pantry") or []),
            budget_exhausted=bool(d.get("budget_exhausted", False)),
        )


def _dish_to_dict(d: Dish) -> dict:
    out = {f.name: getattr(d, f.name) for f in dataclasses.fields(d)}
    out["date"] = d.date.isoformat()
    out["recipe"] = dataclasses.asdict(d.recipe)
    out["recipe"]["ingredients"] = [list(i) for i in d.recipe.ingredients]
    out["lines"] = [
        {"product": dataclasses.asdict(ln.product), "qty": ln.qty,
         "from_ingredient": ln.from_ingredient, "fraction": ln.fraction}
        for ln in d.lines
    ]
    out["pantry"] = list(d.pantry)
    out["not_found"] = list(d.not_found)
    return out


def _dish_from_dict(x: dict) -> Dish:
    r = dict(x["recipe"])
    r["ingredients"] = [tuple(i) for i in r["ingredients"]]
    return Dish(
        date=date.fromisoformat(x["date"]),
        recipe=Recipe(**r),
        cuisine=x["cuisine"],
        is_fish=bool(x["is_fish"]),
        lines=[
            Line(Product(**ln["product"]), int(ln["qty"]), ln["from_ingredient"],
                 ln.get("fraction"))
            for ln in x["lines"]
        ],
        savings=float(x["savings"]),
        persons=int(x["persons"]),
        cook=x.get("cook"),
        oven=x.get("oven"),
        wait=x.get("wait"),
        pantry=list(x.get("pantry") or []),
        not_found=list(x.get("not_found") or []),
        search_term=x.get("search_term") or "",
    )


# --- classification ------------------------------------------------------------------


def cuisine_of(recipe: Recipe, term: str = "") -> str | None:
    """cuisines field, then title words, then ingredient words, then the search term."""
    for c in recipe.cuisines:
        fc = _fold(c)
        for k in CUISINES:
            if fc in k["cuisines"]:
                return k["naam"]
    by_title = title_cuisine(recipe.title)
    if by_title:
        return by_title
    names = [_fold(n or t) for t, n in recipe.ingredients]
    best, best_n = None, 0
    for k in CUISINES:
        n = sum(1 for nm in names if any(w in nm for w in k["ingredienten"]))
        if n > best_n:
            best, best_n = k["naam"], n
    if best:
        return best
    t = _fold(term)
    if t:
        for k in CUISINES:
            if any(w in t for w in k["titel"]):
                return k["naam"]
    return None


def title_cuisine(title: str) -> str | None:
    t = _fold(title)
    for k in CUISINES:
        if any(w in t for w in k["titel"]):
            return k["naam"]
    return None


def _has_word(text: str, words: list[str], exceptions: list[str], stem: str = "") -> bool:
    for w in _words(text):
        if any(w.startswith(x) for x in exceptions):
            continue
        if stem and (w.startswith(stem) or w.endswith(stem)):
            return True
        if any(x in w for x in words):
            return True
    return False


def is_fishy(text: str) -> bool:
    """Fish words, or a word that starts or ends with 'vis' (visfrietjes, koolvis)."""
    return _has_word(text, FISH_WORDS, FISH_EXCEPTIONS, stem="vis")


def _is_chicken(text: str) -> bool:
    return _has_word(text, CHICKEN_WORDS, CHICKEN_EXCEPTIONS)


def core_word(title: str) -> str:
    """The Allerhande search term for a bonus product."""
    words = [w for w in (_words(" ".join(keywords(title)))) if w not in _TITLE_NOISE]
    joined = " ".join(words)
    for cw in CORE_WORDS:  # longest first
        if (" " in cw and cw in joined) or (" " not in cw and any(cw in w for w in words)):
            return cw
    return max(words, key=len) if words else ""


def _is_bonus_food(p: Product) -> bool:
    cat = _fold(p.category or "")
    return bool(cat) and any(c in cat for c in BONUS_CATEGORIES)


def _unit_saving(p: Product) -> float:
    return savings(p, qty_for_deal(p, 1))


def search_terms(bonus: list[Product]) -> list[str]:
    """Core words of the top bonus products (vlees, vis, groente, zuivel) by saving."""
    food = [
        p for p in bonus
        if p.is_bonus and p.orderable and _is_bonus_food(p) and _unit_saving(p) > 0
    ]  # a saving deals.py cannot compute ("100 GRAM VOOR 2.69") is not worth a search
    food.sort(key=lambda p: -_unit_saving(p))  # stable on ties
    terms: list[str] = []
    for p in food[:TOP_BONUS]:
        t = core_word(p.title)
        if t and t not in terms:
            terms.append(t)
    return terms[:MAX_RECIPE_SEARCHES]


def _fresh_count(recipe: Recipe, pantry: list[str]) -> int:
    n = 0
    for text, name in recipe.ingredients:
        if is_pantry(name or text, pantry):
            continue
        if not any(w in _fold(name or text) for w in SHELF_STABLE):
            n += 1
    return n


def _fits(day: date, cook: int | None) -> bool:
    if is_weekend(day):
        return True
    return cook is not None and cook <= MAX_COOK_WEEKDAY


def _blocking(e: Exception) -> bool:
    return isinstance(e, (AhLoginExpired, AhNotJson)) or (
        isinstance(e, AhApiError) and e.status == 429
    )


# --- candidates ----------------------------------------------------------------------


class _BonusOnly:
    """Scoring only looks at the bonus folder: no product searches for every candidate."""

    async def search_products(self, query, size=20):
        return []


@dataclass
class _Cand:
    summary: RecipeSummary
    recipe: Recipe
    term: str
    cuisine: str
    is_fish: bool
    is_chicken: bool
    fresh: int
    score: float
    order: int

    @property
    def id(self) -> int:
        return self.recipe.id

    @property
    def cook(self) -> int | None:
        return self.recipe.cook if self.recipe.cook is not None else self.summary.cook


async def _score(
    recipe: Recipe, bonus: list[Product], pantry: list[str]
) -> tuple[float, list[Product]]:
    parts: dict[int, list[Line]] = {}
    prods: dict[int, Product] = {}
    for text, name in recipe.ingredients:
        if is_pantry(name or text, pantry):
            continue
        amount = parse_ingredient(text, name or text)
        p = await choose_product(_BonusOnly(), amount, bonus)
        if p is None:
            continue
        prods[p.id] = p
        parts.setdefault(p.id, []).append(_line(p, amount, text))
    need = {pid: _packs(lines) for pid, lines in parts.items()}
    return round(sum(_potential(prods[i], n) for i, n in need.items()), 2), list(prods.values())


def _potential(p: Product, n: int) -> float:
    """The deal's saving per pack, times the packs this recipe needs.

    Ranking only: one pack of a "1+1 gratis" product is worth half the deal, because
    another dish may complete it. What is really bought and saved is decided in _finish.
    """
    full = qty_for_deal(p, n)
    return savings(p, full) * n / full if full else 0.0


async def _gather(
    client,
    bonus: list[Product],
    s: Settings,
    exclude: set[int],
    days: list[date],
    detail_budget: int,
    servings: int = SCORE_SERVINGS,
) -> tuple[list[_Cand], int]:
    """Scored candidates, and the number of get_recipe calls it took."""
    lists: list[tuple[str, list[RecipeSummary]]] = []
    for term in search_terms(bonus):
        try:
            found = await client.search_recipes(term)
        except AhApiError as e:
            if _blocking(e):
                raise
            continue
        lists.append((term, [r for r in found if MAIN_COURSE in (r.courses or [])]))

    # round robin over the searches, so the best product does not take every slot
    seen: set[int] = set()
    ordered: list[tuple[str, RecipeSummary]] = []
    longest = max((len(rs) for _, rs in lists), default=0)
    for i in range(longest):
        for term, rs in lists:
            if i >= len(rs):
                continue
            r = rs[i]
            if r.id in seen or r.id in exclude or r.id in s.recent_recipe_ids:
                continue
            seen.add(r.id)
            if any(x in _fold(r.title) for x in EXCLUDE_TITLES):
                continue
            ordered.append((term, r))
    # titles that already name a cuisine first: their get_recipe call is rarely wasted
    ordered.sort(key=lambda tr: title_cuisine(tr[1].title) is None)

    any_weekend = any(is_weekend(d) for d in days)
    cands: list[_Cand] = []
    calls = 0
    for term, summ in ordered:
        if calls >= detail_budget:
            break
        if not any_weekend and not _fits(days[0], summ.cook):
            continue  # cannot be cooked on any of these evenings: do not fetch it
        calls += 1
        try:
            recipe = await client.get_recipe(summ.id, servings=servings)
        except AhApiError as e:
            if _blocking(e):
                raise
            continue
        cuisine = cuisine_of(recipe, term)
        if cuisine is None:
            continue
        score, prods = await _score(recipe, bonus, s.pantry)
        names = " ".join(n or t for t, n in recipe.ingredients)
        fish = (
            is_fishy(recipe.title)
            or is_fishy(names)
            or any(is_fishy(p.title) or _fold(p.category or "") == "vis" for p in prods)
        )
        cands.append(
            _Cand(
                summary=summ, recipe=recipe, term=term, cuisine=cuisine, is_fish=fish,
                is_chicken=_is_chicken(recipe.title) or _is_chicken(names),
                fresh=_fresh_count(recipe, s.pantry), score=score, order=len(cands),
            )
        )
    return cands, calls


# --- picking the menu ------------------------------------------------------------------


class _OutOfBudget(Exception):
    pass


def _rank(day: date):
    if is_weekend(day):  # weekend cooking: on equal savings the longer recipe wins
        return lambda c: (-c.score, -(c.cook or 0), c.order)
    return lambda c: (-c.score, c.order)


def _pick(
    cands: list[_Cand], days: list[date], budget: list[int], best: list
) -> list[_Cand] | None:
    """Greedy in date order with backtracking; the first full menu found.

    `budget` is shared over all attempts; when it runs out _OutOfBudget is raised.
    `best[0]` keeps the longest valid partial menu seen, as (days, chosen).
    """
    need = min(MIN_CUISINES, len(days))
    if len({c.cuisine for c in cands}) < need:
        return None
    per_day = [sorted((c for c in cands if _fits(d, c.cook)), key=_rank(d)) for d in days]
    # cuisines still on offer from evening i on, from non-fish and from fish recipes
    plain: list[set] = [set() for _ in range(len(days) + 1)]
    fishy: list[set] = [set() for _ in range(len(days) + 1)]
    for i in range(len(days) - 1, -1, -1):
        plain[i] = plain[i + 1] | {c.cuisine for c in per_day[i] if not c.is_fish}
        fishy[i] = fishy[i + 1] | {c.cuisine for c in per_day[i] if c.is_fish}
    at_day = [{c.cuisine for c in cs} for cs in per_day]

    def rec(i: int, path: list[_Cand], fish: int) -> list[_Cand] | None:
        distinct = {c.cuisine for c in path}
        valid = len(distinct) >= min(MIN_CUISINES, len(path))
        if path and valid and len(path) > len(best[0][1]):
            best[0] = (days[: len(path)], list(path))
        if i == len(days):
            return []
        new_plain = plain[i] - distinct
        new_fish = fishy[i] - distinct - new_plain
        reachable = len(new_plain) + min(len(new_fish), MAX_FISH - fish)
        missing = need - len(distinct)
        if reachable < missing:
            return None  # this week can no longer reach enough cuisines
        if missing > 0 and sum(1 for j in range(i, len(days)) if at_day[j] - distinct) < missing:
            return None  # not enough evenings left that can take a new cuisine
        only_new = missing >= len(days) - i
        used = {c.id for c in path}
        for c in per_day[i]:
            if c.id in used or (c.is_fish and fish >= MAX_FISH):
                continue
            if only_new and c.cuisine in distinct:
                continue
            budget[0] -= 1
            if budget[0] < 0:
                raise _OutOfBudget
            rest = rec(i + 1, path + [c], fish + c.is_fish)
            if rest is not None:
                return [c] + rest
        return None

    return rec(0, [], 0)


def _arrange(chosen: list[_Cand], days: list[date], evenings: list[date]) -> list[_Cand]:
    """Fish and chicken on the first two evenings, Monday and Tuesday the least fresh
    recipes, the longest recipes in the weekend. Weekday cooking time stays <= 45."""
    first_two = set(evenings[:2])

    def key(perm):
        early = sum(
            1 for d, c in zip(days, perm) if d in first_two and (c.is_fish or c.is_chicken)
        )
        late_fresh = sum(c.fresh for d, c in zip(days, perm) if d.weekday() in (0, 1))
        weekend_cook = sum(c.cook or 0 for d, c in zip(days, perm) if is_weekend(d))
        return (early, -late_fresh, weekend_cook)

    best, best_key = chosen, key(chosen)
    for perm in itertools.permutations(chosen):
        if not all(_fits(d, c.cook) for d, c in zip(days, perm)):
            continue
        k = key(perm)
        if k > best_key:
            best, best_key = list(perm), k
    return best


def _choose_menu(cands: list[_Cand], evenings: list[date]) -> tuple[dict[date, _Cand], bool]:
    """Fill as many evenings as the rules allow; drop the latest evenings first.

    Returns the menu and whether the search budget ran out. Then the longest valid
    partial menu found so far is used: the rules hold, but it may not be the best menu.
    """
    budget = [_SEARCH_BUDGET]
    best: list = [((), [])]
    try:
        for k in range(len(evenings), 0, -1):
            for days in itertools.combinations(evenings, k):
                chosen = _pick(cands, list(days), budget, best)
                if chosen:
                    return dict(zip(days, _arrange(chosen, list(days), evenings))), False
    except _OutOfBudget:
        days, chosen = best[0]
        log.warning(
            "menu search budget spent: using the best partial menu (%d of %d evenings)",
            len(chosen), len(evenings),
        )
        if not chosen:
            return {}, True
        return dict(zip(days, _arrange(chosen, list(days), evenings))), True
    return {}, False


# --- building dishes and the basket ------------------------------------------------------


async def _lines(client, recipe: Recipe, resolve) -> tuple[list[Line], list[str], list[str]]:
    lines, pantry, not_found = [], [], []
    for i, (text, name) in enumerate(recipe.ingredients):
        kind, p = await resolve(i, text, name)
        if kind == "pantry":
            pantry.append(text)
            continue
        if p is None:
            not_found.append(text)
            continue
        lines.append(_line(p, parse_ingredient(text, name or text), text))
    return lines, pantry, not_found


def _line(p: Product, amount, text: str) -> Line:
    # most bonus cards have no salesUnitSize; the title often says it ("... 3 kilo")
    size = parse_unit_size(p.unit_size) or parse_unit_size(p.title)
    return Line(p, packs_needed(amount, size), text, pack_fraction(amount, size))


def _packs(lines: list[Line]) -> int:
    """Packs for these lines of one product together: amounts first, then round up."""
    parts = sum(ln.fraction for ln in lines if ln.fraction is not None)
    whole = sum(ln.qty for ln in lines if ln.fraction is None)
    return max(1, math.ceil(parts - 1e-9) + whole) if lines else 0


def _resolver(client, bonus: list[Product], pantry: list[str], cache: dict):
    async def resolve(_i: int, text: str, name: str):
        if is_pantry(name or text, pantry):
            return "pantry", None
        amount = parse_ingredient(text, name or text)
        return "product", await choose_product(client, amount, bonus, cache)

    return resolve


async def _make_dish(
    client, c: _Cand, day: date, persons: int, resolve, budget: list[int] | None = None
) -> Dish:
    recipe = c.recipe
    if persons != recipe.servings:
        if budget is None or budget[0] > 0:
            if budget is not None:
                budget[0] -= 1
            try:
                recipe = await client.get_recipe(c.id, servings=persons)
            except AhApiError as e:
                if _blocking(e):
                    raise
        if recipe is c.recipe:
            persons = recipe.servings  # could not scale: say honestly for how many it is
    lines, pantry, not_found = await _lines(client, recipe, resolve)
    return Dish(
        date=day, recipe=recipe, cuisine=c.cuisine, is_fish=c.is_fish, lines=lines,
        savings=0.0, persons=persons, cook=c.cook, oven=c.summary.oven, wait=c.summary.wait,
        pantry=pantry, not_found=not_found, search_term=c.term,
    )


def _finish(
    dishes: list[Dish], short_by: int, pantry: list[str], budget_exhausted: bool = False
) -> Plan:
    """Merge quantities per product, top up deals only for shared products, split savings."""
    dishes = sorted(dishes, key=lambda d: d.date)
    per_product: dict[int, list[Line]] = {}
    users: dict[int, set[int]] = {}
    products: dict[int, Product] = {}
    for i, d in enumerate(dishes):
        for ln in d.lines:
            products.setdefault(ln.product.id, ln.product)
            per_product.setdefault(ln.product.id, []).append(ln)
            users.setdefault(ln.product.id, set()).add(i)
    need = {pid: _packs(lines) for pid, lines in per_product.items()}
    # each dish's share of a product, by its own amount
    weight_total = {pid: sum(_weight(ln) for ln in ls) for pid, ls in per_product.items()}
    # Ruling 19: only buy what gets used; the one exception is a free extra unit
    # ("1+1 gratis", "2e gratis") for a product that more than one dish uses
    basket = {
        pid: (qty_for_deal(products[pid], n) if len(users[pid]) > 1
              and _free_extra(products[pid]) else n)
        for pid, n in need.items()
    }
    total_saving = {pid: savings(products[pid], q) for pid, q in basket.items()}
    out = []
    for d in dishes:
        mine: dict[int, float] = {}
        for ln in d.lines:
            mine[ln.product.id] = mine.get(ln.product.id, 0) + _weight(ln)
        share = sum(
            total_saving[pid] * w / weight_total[pid]
            for pid, w in mine.items() if weight_total[pid]
        )
        out.append(dataclasses.replace(d, savings=round(share, 2)))
    not_found: list[str] = []
    for d in out:
        for t in d.not_found:
            if t not in not_found:
                not_found.append(t)
    return Plan(
        dishes=out,
        basket=basket,
        products=products,
        savings=round(sum(total_saving.values()), 2),
        not_found=not_found,
        short_by=short_by,
        pantry=list(pantry),
        budget_exhausted=budget_exhausted,
    )


def _free_extra(p: Product) -> bool:
    """A deal whose extra units cost nothing: x+y gratis, or the nth one free."""
    d = parse_mechanism(p.mechanism)
    return d.kind == "x_plus_y" or (d.kind == "nth_discount" and d.pct >= 100)


def _weight(ln: Line) -> float:
    return ln.fraction if ln.fraction is not None else float(ln.qty)


def score_servings(s: Settings, days: list[date]) -> int:
    """The most common number of persons over the evenings (ties: the higher one)."""
    counts: dict[int, int] = {}
    for d in days:
        n = s.persons.get(d, SCORE_SERVINGS)
        counts[n] = counts.get(n, 0) + 1
    return max(counts, key=lambda n: (counts[n], n)) if counts else SCORE_SERVINGS


# --- public API -----------------------------------------------------------------------------


async def build_plan(client, bonus: list[Product], s: Settings) -> Plan:
    evenings = sorted(s.evenings)
    detail_budget = max(MAX_RECIPE_DETAILS - len(evenings), 0)
    cands, used = await _gather(
        client, bonus, s, set(), evenings, detail_budget, score_servings(s, evenings)
    )
    menu, exhausted = _choose_menu(cands, evenings)
    budget = [MAX_RECIPE_DETAILS - used]  # what is left for scaling to the persons
    resolve = _resolver(client, bonus, s.pantry, {})
    dishes = [
        await _make_dish(client, c, day, s.persons.get(day, SCORE_SERVINGS), resolve, budget)
        for day, c in sorted(menu.items())
    ]
    return _finish(dishes, len(evenings) - len(dishes), s.pantry, exhausted)


async def swap_dish(
    client, plan: Plan, day: date, bonus: list[Product], s: Settings, exclude: set[int]
) -> Plan:
    """Best other recipe for `day` under the same rules; the rest of the week stays."""
    old = next((d for d in plan.dishes if d.date == day), None)
    others = [d for d in plan.dishes if d.date != day]
    skip = set(exclude) | {d.recipe.id for d in others} | ({old.recipe.id} if old else set())
    persons = old.persons if old else s.persons.get(day, SCORE_SERVINGS)
    cands, _ = await _gather(client, bonus, s, skip, [day], MAX_RECIPE_DETAILS - 1, persons)
    need = min(MIN_CUISINES, len(others) + 1)
    other_cuisines = {d.cuisine for d in others}
    other_fish = sum(d.is_fish for d in others)
    ok = [
        c for c in cands
        if _fits(day, c.cook)
        and not (c.is_fish and other_fish >= MAX_FISH)
        and len(other_cuisines | {c.cuisine}) >= need
    ]
    if not ok:
        return dataclasses.replace(plan, short_by=1)
    best = min(ok, key=_rank(day))
    resolve = _resolver(client, bonus, s.pantry, {})
    dish = await _make_dish(client, best, day, persons, resolve)
    dishes = others + [dish]
    filled = {d.date for d in dishes}
    return _finish(
        dishes, sum(1 for e in s.evenings if e not in filled), s.pantry, plan.budget_exhausted
    )


async def rescale_dish(client, plan: Plan, day: date, persons: int) -> Plan:
    """The same recipe for another number of persons; only this dish changes."""
    old = next((d for d in plan.dishes if d.date == day), None)
    if old is None or persons < 1:
        return plan
    recipe = await client.get_recipe(old.recipe.id, servings=persons)

    old_keys = [n or t for t, n in old.recipe.ingredients]
    key_of_text = dict(zip((t for t, _ in old.recipe.ingredients), old_keys))
    by_key = {key_of_text.get(ln.from_ingredient, ""): ln.product for ln in old.lines}
    missing_keys = {key_of_text.get(t, t) for t in old.not_found}
    same_list = len(old_keys) == len(recipe.ingredients)
    known_bonus = [p for p in plan.products.values() if p.is_bonus]

    async def resolve(i: int, text: str, name: str):
        if is_pantry(name or text, plan.pantry):
            return "pantry", None  # never bought, also not after scaling
        # the scaled text differs ("4 el" for "2 el"): match on name, else on position
        key = name or (old_keys[i] if same_list else text)
        if key in by_key:
            return "product", by_key[key]
        if key in missing_keys:
            return "product", None
        return "product", await choose_product(
            client, parse_ingredient(text, name or text), known_bonus
        )

    lines, pantry, not_found = await _lines(client, recipe, resolve)
    dish = dataclasses.replace(
        old, recipe=recipe, persons=persons, lines=lines, pantry=pantry, not_found=not_found
    )
    dishes = [dish if d.date == day else d for d in plan.dishes]
    return _finish(dishes, plan.short_by, plan.pantry, plan.budget_exhausted)


async def restore_dish(
    client, plan: Plan, day: date, recipe_id: int, bonus: list[Product]
) -> Plan:
    """Put recipe `recipe_id` back on `day` ("Terug" after a swap); the rest stays.

    The recipe already passed the week's rules before it was swapped out, so they are
    not checked again. Oven and waiting times only come with search results and are
    lost here.
    """
    old = next((d for d in plan.dishes if d.date == day), None)
    persons = old.persons if old else SCORE_SERVINGS
    recipe = await client.get_recipe(recipe_id, servings=persons)
    names = " ".join(n or t for t, n in recipe.ingredients)
    resolve = _resolver(client, bonus, plan.pantry, {})
    lines, pantry, not_found = await _lines(client, recipe, resolve)
    fish = is_fishy(recipe.title) or is_fishy(names) or any(
        is_fishy(ln.product.title) or _fold(ln.product.category or "") == "vis" for ln in lines
    )
    dish = Dish(
        date=day, recipe=recipe, cuisine=cuisine_of(recipe) or (old.cuisine if old else ""),
        is_fish=fish, lines=lines, savings=0.0, persons=persons, cook=recipe.cook,
        pantry=pantry, not_found=not_found,
    )
    dishes = [d for d in plan.dishes if d.date != day] + [dish]
    return _finish(dishes, plan.short_by, plan.pantry, plan.budget_exhausted)
