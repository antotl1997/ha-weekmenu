"""The week's dinners: dishes made from the recipes the user picks, matched to AH
products around the bonus, and the basket and savings of the whole menu.

The add-on never chooses a menu by itself (v1.1). What is left of choosing: the search
terms for the best bonus products and the bonus score of a recipe, which fill the
choice list (catalog.py).
"""

from __future__ import annotations

import dataclasses
import logging
import math
import re
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import yaml

from app.ahclient import AhApiError, Product, Recipe
from app.recipes import is_own, scale_recipe
from app.deals import parse_mechanism, qty_for_deal, savings
from app.ingredients import (
    PRODUCT,
    SKIP,
    choose_product,
    is_pantry,
    keywords,
    override_for,
    override_key,
    stored_product,
    pack_fraction,
    packs_needed,
    parse_ingredient,
    parse_unit_size,
    weight_priced,
)

MAX_COOK_WEEKDAY = 45
TOP_BONUS = 25
MAX_RECIPE_SEARCHES = 25
SCORE_SERVINGS = 2  # default persons when an evening has none
MAIN_COURSE = "hoofdgerecht"

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
    # cook day -> its leftover night; persons of a cook day already include that night
    leftovers: dict[date, date] = field(default_factory=dict)
    # v0.9: "Klopt niet" choices (override_key -> choice), before every heuristic
    overrides: dict = field(default_factory=dict)
    # v1.0 (taste.json): recipes never chosen again, and recipes with a thumb down
    never: set[int] = field(default_factory=set)
    down: set[int] = field(default_factory=set)


@dataclass
class Line:
    product: Product
    qty: int  # whole packs for this dish alone
    from_ingredient: str
    fraction: float | None = None  # the amount as part of a pack; None: not convertible
    check: bool = False  # v0.9: priced per weight without a pack size: 1 piece, check it


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
    leftover_date: date | None = None  # the leftover night eaten from this dish
    skipped: list[str] = field(default_factory=list)  # v0.9: "Hoeft niet" texts


@dataclass
class Plan:
    dishes: list[Dish]
    basket: dict[int, int]
    products: dict[int, Product]
    savings: float
    not_found: list[str]
    short_by: int
    pantry: list[str] = field(default_factory=list)  # the pantry list the plan was made with
    budget_exhausted: bool = False  # before v1.1: the menu search ran out of budget
    removed_days: list[date] = field(default_factory=list)  # "Gerecht weghalen": no dish

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
            "removed_days": [d.isoformat() for d in self.removed_days],
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
            removed_days=[date.fromisoformat(x) for x in d.get("removed_days") or []],
        )


def _dish_to_dict(d: Dish) -> dict:
    out = {f.name: getattr(d, f.name) for f in dataclasses.fields(d)}
    out["date"] = d.date.isoformat()
    out["leftover_date"] = d.leftover_date.isoformat() if d.leftover_date else None
    out["recipe"] = dataclasses.asdict(d.recipe)
    out["recipe"]["ingredients"] = [list(i) for i in d.recipe.ingredients]
    out["lines"] = [
        {"product": dataclasses.asdict(ln.product), "qty": ln.qty,
         "from_ingredient": ln.from_ingredient, "fraction": ln.fraction,
         "check": ln.check}
        for ln in d.lines
    ]
    out["pantry"] = list(d.pantry)
    out["not_found"] = list(d.not_found)
    out["skipped"] = list(d.skipped)
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
                 ln.get("fraction"), bool(ln.get("check", False)))
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
        leftover_date=date.fromisoformat(x["leftover_date"]) if x.get("leftover_date") else None,
        skipped=list(x.get("skipped") or []),
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


# --- scoring -------------------------------------------------------------------------


class _BonusOnly:
    """Scoring only looks at the bonus folder: no product searches for every candidate."""

    async def search_products(self, query, size=20):
        return []


async def _score(
    recipe: Recipe, bonus: list[Product], pantry: list[str], overrides: dict | None = None,
) -> tuple[float, list[Product]]:
    parts: dict[int, list[Line]] = {}
    prods: dict[int, Product] = {}
    for text, name in recipe.ingredients:
        if is_pantry(name or text, pantry, overrides):
            continue
        amount = parse_ingredient(text, name or text)
        p = await choose_product(_BonusOnly(), amount, bonus, overrides=overrides)
        if p is None:
            continue
        prods[p.id] = p
        parts.setdefault(p.id, []).append(_line(p, amount, text))
    need = {pid: _packs(lines) for pid, lines in parts.items()}
    return round(sum(_potential(prods[i], n) for i, n in need.items()), 2), list(prods.values())


async def score_recipe(
    recipe: Recipe, bonus: list[Product], pantry: list[str], overrides: dict | None = None,
    term: str = "",
) -> tuple[float, str | None, bool]:
    """The bonus saving of `recipe` against the folder `bonus`, its cuisine (None: none
    found; `term` is the search that found it) and whether it is a fish dish. Local
    only: no AH call."""
    score, prods = await _score(recipe, bonus, pantry, overrides)
    names = " ".join(n or t for t, n in recipe.ingredients)
    fish = (
        is_fishy(recipe.title)
        or is_fishy(names)
        or any(is_fishy(p.title) or _fold(p.category or "") == "vis" for p in prods)
    )
    return score, cuisine_of(recipe, term), fish


def _potential(p: Product, n: int) -> float:
    """The deal's saving per pack, times the packs this recipe needs.

    Ranking only: one pack of a "1+1 gratis" product is worth half the deal, because
    another dish may complete it. What is really bought and saved is decided in _finish.
    """
    full = qty_for_deal(p, n)
    return savings(p, full) * n / full if full else 0.0


# --- building dishes and the basket ------------------------------------------------------


async def _lines(client, recipe: Recipe, resolve):
    """(lines, pantry texts, not found texts, "Hoeft niet" texts) of a recipe."""
    lines, pantry, not_found, skipped = [], [], [], []
    for i, (text, name) in enumerate(recipe.ingredients):
        kind, p = await resolve(i, text, name)
        if kind == "pantry":
            pantry.append(text)
            continue
        if kind == "skip":
            skipped.append(text)
            continue
        if p is None:
            not_found.append(text)
            continue
        lines.append(_line(p, parse_ingredient(text, name or text), text))
    return lines, pantry, not_found, skipped


def _line(p: Product, amount, text: str) -> Line:
    if weight_priced(p):  # "per 100 gram" and no pack size: never several to reach weight
        return Line(p, 1, text, None, check=True)
    # most bonus cards have no salesUnitSize; the title often says it ("... 3 kilo")
    size = parse_unit_size(p.unit_size) or parse_unit_size(p.title)
    return Line(p, packs_needed(amount, size), text, pack_fraction(amount, size))


def _packs(lines: list[Line]) -> int:
    """Packs for these lines of one product together: amounts first, then round up."""
    parts = sum(ln.fraction for ln in lines if ln.fraction is not None)
    whole = sum(ln.qty for ln in lines if ln.fraction is None)
    return max(1, math.ceil(parts - 1e-9) + whole) if lines else 0


def _resolver(client, bonus: list[Product], pantry: list[str], cache: dict,
              overrides: dict | None = None):
    async def resolve(_i: int, text: str, name: str):
        ov = override_for(name or text, overrides)
        if ov is not None and ov["kind"] == SKIP:
            return "skip", None
        if is_pantry(name or text, pantry, overrides):
            return "pantry", None
        amount = parse_ingredient(text, name or text)
        return "product", await choose_product(client, amount, bonus, cache, overrides)

    return resolve


def _finish(
    dishes: list[Dish], short_by: int, pantry: list[str], budget_exhausted: bool = False,
    removed_days=(),
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
        removed_days=sorted(removed_days),
    )


def _free_extra(p: Product) -> bool:
    """A deal whose extra units cost nothing: x+y gratis, or the nth one free."""
    d = parse_mechanism(p.mechanism)
    return d.kind == "x_plus_y" or (d.kind == "nth_discount" and d.pct >= 100)


def _weight(ln: Line) -> float:
    return ln.fraction if ln.fraction is not None else float(ln.qty)


# --- public API -----------------------------------------------------------------------------


async def rescale_dish(client, plan: Plan, day: date, persons: int,
                       overrides: dict | None = None) -> Plan:
    """The same recipe for another number of persons; only this dish changes. An own
    recipe is scaled from the copy in the dish (no store read, no AH call)."""
    old = next((d for d in plan.dishes if d.date == day), None)
    if old is None or persons < 1:
        return plan
    started = time.perf_counter()
    if is_own(old.recipe.id):  # from the dish's own copy, never from the store
        recipe = scale_recipe(old.recipe, persons)
    else:
        recipe = await client.get_recipe(old.recipe.id, servings=persons)

    old_keys = [n or t for t, n in old.recipe.ingredients]
    key_of_text = dict(zip((t for t, _ in old.recipe.ingredients), old_keys))
    by_key = {key_of_text.get(ln.from_ingredient, ""): ln.product for ln in old.lines}
    missing_keys = {key_of_text.get(t, t) for t in old.not_found}
    same_list = len(old_keys) == len(recipe.ingredients)
    known_bonus = [p for p in plan.products.values() if p.is_bonus]

    async def resolve(i: int, text: str, name: str):
        ov = override_for(name or text, overrides)
        if ov is not None and ov["kind"] == SKIP:
            return "skip", None
        if is_pantry(name or text, plan.pantry, overrides):
            return "pantry", None  # never bought, also not after scaling
        if ov is not None and ov["kind"] == PRODUCT:
            return "product", await choose_product(
                client, parse_ingredient(text, name or text), known_bonus, overrides=overrides)
        # the scaled text differs ("4 el" for "2 el"): match on name, else on position
        key = name or (old_keys[i] if same_list else text)
        if key in by_key:
            return "product", by_key[key]
        if key in missing_keys:
            return "product", None
        return "product", await choose_product(
            client, parse_ingredient(text, name or text), known_bonus
        )

    lines, pantry, not_found, skipped = await _lines(client, recipe, resolve)
    dish = dataclasses.replace(
        old, recipe=recipe, persons=persons, lines=lines, pantry=pantry, not_found=not_found,
        skipped=skipped,
    )
    dishes = [dish if d.date == day else d for d in plan.dishes]
    log.info("Planner: %d personen in %.2f s", persons, time.perf_counter() - started)
    return _finish(dishes, plan.short_by, plan.pantry, plan.budget_exhausted,
                   plan.removed_days)


def dish_to_dict(d: Dish) -> dict:
    return _dish_to_dict(d)


def dish_from_dict(x: dict) -> Dish:
    return _dish_from_dict(x)


def put_dish(plan: Plan, dish: Dish) -> Plan:
    """Put a saved dish back exactly as it was ("Terug"); no AH calls at all.

    Same recipe, products, quantities, times; basket and savings are recomputed.
    """
    dishes = [d for d in plan.dishes if d.date != dish.date] + [dish]
    back = {dish.date, dish.leftover_date}
    return _finish(dishes, plan.short_by, plan.pantry, plan.budget_exhausted,
                   [x for x in plan.removed_days if x not in back])


def remove_dish(plan: Plan, day: date) -> Plan:
    """"Gerecht weghalen": `day` gets no dish and nothing in its place; no AH calls.

    Its leftover night lapses with it: both evenings become "geen gerecht". Basket
    and savings are recomputed without it. Without a dish on `day` the plan is
    returned as it is.
    """
    old = next((d for d in plan.dishes if d.date == day), None)
    if old is None:
        return plan
    dishes = [d for d in plan.dishes if d.date != day]
    gone = {day} | ({old.leftover_date} if old.leftover_date else set())
    return _finish(dishes, plan.short_by, plan.pantry, plan.budget_exhausted,
                   set(plan.removed_days) | gone)


async def dish_for(
    client, recipe_id: int, day: date, persons: int, bonus: list[Product],
    pantry: list[str], overrides: dict | None = None, leftover_date: date | None = None,
) -> Dish:
    """The dish of recipe `recipe_id` for `persons` on `day`, its ingredients matched
    to products (savings 0: `_finish` splits them). Own recipes (negative ids) come
    from the store when `client` is a LocalFirst. The week's rules are not checked:
    the user chose this recipe. A recipe without a known cuisine is "eigen".
    """
    recipe = await client.get_recipe(recipe_id, servings=persons)
    return await _dish_of(client, recipe, day, persons, bonus, pantry, overrides,
                          leftover_date)


async def _dish_of(
    client, recipe: Recipe, day: date, persons: int, bonus: list[Product],
    pantry: list[str], overrides: dict | None, leftover_date: date | None,
) -> Dish:
    """The dish of this (already scaled) recipe; see dish_for."""
    names = " ".join(n or t for t, n in recipe.ingredients)
    resolve = _resolver(client, bonus, pantry, {}, overrides)
    lines, pantry_texts, not_found, skipped = await _lines(client, recipe, resolve)
    fish = is_fishy(recipe.title) or is_fishy(names) or any(
        is_fishy(ln.product.title) or _fold(ln.product.category or "") == "vis" for ln in lines
    )
    return Dish(
        date=day, recipe=recipe, cuisine=cuisine_of(recipe) or "eigen", is_fish=fish,
        lines=lines, savings=0.0, persons=persons, cook=recipe.cook, oven=recipe.oven,
        wait=recipe.wait, pantry=pantry_texts, not_found=not_found,
        leftover_date=leftover_date, skipped=skipped,
    )


def _keep_cuisine(new: Dish, old: Dish | None) -> Dish:
    """An AH recipe whose cuisine came from its search term keeps the old label."""
    if old is not None and cuisine_of(new.recipe) is None:
        return dataclasses.replace(new, cuisine=old.cuisine)
    return new


def _with_dish(plan: Plan, dish: Dish) -> Plan:
    """`plan` with `dish` on its day (replacing what was there); its evenings are no
    longer "geen gerecht"."""
    dishes = [d for d in plan.dishes if d.date != dish.date] + [dish]
    back = {dish.date, dish.leftover_date}
    return _finish(dishes, plan.short_by, plan.pantry, plan.budget_exhausted,
                   [x for x in plan.removed_days if x not in back])


def empty_plan(pantry: list[str]) -> Plan:
    """A menu without dishes, for a new draft the user fills evening by evening."""
    return _finish([], 0, pantry)


async def pick_dish(
    client, plan: Plan, day: date, recipe_id: int, bonus: list[Product], persons: int,
    leftover_date: date | None, overrides: dict | None = None,
) -> Plan:
    """The user picked recipe `recipe_id` for `day`: it replaces the dish there (or
    fills the empty evening); the rest of the week stays as it is."""
    started = time.perf_counter()
    dish = await dish_for(client, recipe_id, day, persons, bonus, plan.pantry, overrides,
                          leftover_date)
    log.info("Planner: gekozen gerecht in %.2f s", time.perf_counter() - started)
    return _with_dish(plan, dish)


async def rebuild_plan(client, plan: Plan, bonus: list[Product],
                       overrides: dict | None = None) -> Plan:
    """"Menu bijwerken": every dish made again from the same recipe, day, persons and
    leftover night, against `bonus` (the new folder). An own recipe is rebuilt from the
    copy in the dish, so editing or deleting it never changes the menu. An AH recipe
    that is gone (404) keeps its dish as it was; other AH errors are raised."""
    started = time.perf_counter()
    dishes = []
    for old in plan.dishes:
        if is_own(old.recipe.id):  # the dish's own copy: the store is never read again
            new = await _dish_of(client, old.recipe, old.date, old.persons, bonus,
                                 plan.pantry, overrides, old.leftover_date)
            dishes.append(_keep_cuisine(new, old))
            continue
        try:
            new = await dish_for(client, old.recipe.id, old.date, old.persons, bonus,
                                 plan.pantry, overrides, old.leftover_date)
        except AhApiError as e:
            if e.status != 404:
                raise
            dishes.append(old)
            continue
        dishes.append(_keep_cuisine(new, old))
    log.info("Planner: menu bijgewerkt in %.2f s", time.perf_counter() - started)
    return _finish(dishes, plan.short_by, plan.pantry, plan.budget_exhausted,
                   plan.removed_days)


async def restore_dish(
    client, plan: Plan, day: date, recipe_id: int, bonus: list[Product],
    overrides: dict | None = None,
) -> Plan:
    """Put recipe `recipe_id` back on `day`: the fallback for logs without a saved dish.

    The recipe already passed the week's rules before it was swapped out, so they are
    not checked again.
    """
    old = next((d for d in plan.dishes if d.date == day), None)
    persons = old.persons if old else SCORE_SERVINGS
    dish = await dish_for(client, recipe_id, day, persons, bonus, plan.pantry, overrides,
                          old.leftover_date if old else None)
    return _with_dish(plan, _keep_cuisine(dish, old))


async def _redo(plan: Plan, key: str, resolve) -> Plan:
    """Every ingredient with this key, in every dish, resolved again by `resolve`
    (text, name) -> (kind, product); nothing else changes. `plan` itself when no dish
    has the ingredient."""
    hit_any = False
    dishes = []
    for d in plan.dishes:
        hit = [(t, n) for t, n in d.recipe.ingredients if override_key(n or t) == key]
        if not hit:
            dishes.append(d)
            continue
        hit_any = True
        texts = {t for t, _ in hit}
        lines = [ln for ln in d.lines if ln.from_ingredient not in texts]
        pantry = [x for x in d.pantry if x not in texts]
        not_found = [x for x in d.not_found if x not in texts]
        skipped = [x for x in d.skipped if x not in texts]
        for text, name in hit:
            kind, p = await resolve(text, name)
            if kind == "skip":
                skipped.append(text)
            elif kind == "pantry":
                pantry.append(text)
            elif p is None:
                not_found.append(text)
            else:
                lines.append(_line(p, parse_ingredient(text, name or text), text))
        order = {t: i for i, (t, _) in enumerate(d.recipe.ingredients)}
        lines.sort(key=lambda ln: order.get(ln.from_ingredient, len(order)))
        not_found.sort(key=lambda t: order.get(t, len(order)))
        dishes.append(dataclasses.replace(d, lines=lines, pantry=pantry, not_found=not_found,
                                          skipped=skipped))
    if not hit_any:
        return plan
    return _finish(dishes, plan.short_by, plan.pantry, plan.budget_exhausted,
                   plan.removed_days)


async def apply_override(plan: Plan, key: str, overrides: dict,
                         current: Product | None = None) -> Plan:
    """A "Klopt niet" choice on the menu as it is, without any AH call. For "Ander
    product" the product as this week knows it: `current` (just found by the search),
    else the plan's own copy, else the stored one (no deal).

    Returns `plan` itself when no dish has the ingredient (or there is no choice).
    """
    ov = overrides.get(key)
    if ov is None:
        return plan

    async def resolve(text, name):
        if ov["kind"] != PRODUCT:
            return ov["kind"], None
        chosen = stored_product(ov["product"])
        if current is not None and current.id == chosen.id:
            return PRODUCT, current
        return PRODUCT, plan.products.get(chosen.id) or chosen

    return await _redo(plan, key, resolve)


async def reresolve(client, plan: Plan, key: str, bonus: list[Product],
                    overrides: dict) -> Plan:
    """After "Terugdraaien": the add-on chooses again for this ingredient, as it would
    in a new menu (pantry list, bonus, AH search), with the remaining choices."""
    resolve_one = _resolver(client, bonus, plan.pantry, {}, overrides)

    async def resolve(text, name):
        return await resolve_one(0, text, name)

    return await _redo(plan, key, resolve)
