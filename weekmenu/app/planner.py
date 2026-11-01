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
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import yaml

from app.ahclient import AhApiError, AhLoginExpired, AhNotJson, Product, Recipe, RecipeSummary
from app.deals import qty_for_deal, savings
from app.ingredients import (
    choose_product,
    is_pantry,
    keywords,
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
SCORE_SERVINGS = 2
MAIN_COURSE = "hoofdgerecht"
# Backtracking budget for picking the menu (nodes per attempt); plenty for <= 40 x 7.
_SEARCH_BUDGET = 20000

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
    qty: int
    from_ingredient: str


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

    def to_dict(self) -> dict:
        return {
            "dishes": [_dish_to_dict(d) for d in self.dishes],
            "basket": {str(k): v for k, v in self.basket.items()},
            "products": {str(k): dataclasses.asdict(p) for k, p in self.products.items()},
            "savings": self.savings,
            "not_found": list(self.not_found),
            "short_by": self.short_by,
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
        )


def _dish_to_dict(d: Dish) -> dict:
    out = {f.name: getattr(d, f.name) for f in dataclasses.fields(d)}
    out["date"] = d.date.isoformat()
    out["recipe"] = dataclasses.asdict(d.recipe)
    out["recipe"]["ingredients"] = [list(i) for i in d.recipe.ingredients]
    out["lines"] = [
        {"product": dataclasses.asdict(ln.product), "qty": ln.qty,
         "from_ingredient": ln.from_ingredient}
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
            Line(Product(**ln["product"]), int(ln["qty"]), ln["from_ingredient"])
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
    title = _fold(recipe.title)
    for k in CUISINES:
        if any(w in title for w in k["titel"]):
            return k["naam"]
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


def search_terms(bonus: list[Product]) -> list[str]:
    """Core words of the top bonus products (vlees, vis, groente, zuivel) by saving."""
    food = [p for p in bonus if p.is_bonus and p.orderable and _is_bonus_food(p)]
    food.sort(key=lambda p: -savings(p, qty_for_deal(p, 1)))  # stable on ties
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
    need: dict[int, int] = {}
    prods: dict[int, Product] = {}
    for text, name in recipe.ingredients:
        if is_pantry(name or text, pantry):
            continue
        amount = parse_ingredient(text, name or text)
        p = await choose_product(_BonusOnly(), amount, bonus)
        if p is None:
            continue
        prods[p.id] = p
        need[p.id] = need.get(p.id, 0) + packs_needed(amount, parse_unit_size(p.unit_size))
    score = round(sum(savings(prods[i], q) for i, q in need.items()), 2)
    return score, list(prods.values())


async def _gather(
    client,
    bonus: list[Product],
    s: Settings,
    exclude: set[int],
    days: list[date],
    detail_budget: int,
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
            recipe = await client.get_recipe(summ.id, servings=SCORE_SERVINGS)
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


def _pick(cands: list[_Cand], days: list[date]) -> list[_Cand] | None:
    """Greedy in date order with backtracking; the first full menu found."""
    need = min(MIN_CUISINES, len(days))
    if len({c.cuisine for c in cands}) < need:
        return None
    per_day = [sorted((c for c in cands if _fits(d, c.cook)), key=_rank(d)) for d in days]
    # cuisines still on offer from evening i on: prunes weeks that cannot reach `need`
    avail = [set() for _ in range(len(days) + 1)]
    for i in range(len(days) - 1, -1, -1):
        avail[i] = avail[i + 1] | {c.cuisine for c in per_day[i]}
    nodes = 0

    def rec(i: int, used: set[int], cuisines: list[str], fish: int) -> list[_Cand] | None:
        nonlocal nodes
        if i == len(days):
            return []
        distinct = set(cuisines)
        if len(avail[i] - distinct) < need - len(distinct):
            return None
        only_new = need - len(distinct) >= len(days) - i
        for c in per_day[i]:
            if c.id in used or (c.is_fish and fish >= MAX_FISH):
                continue
            if only_new and c.cuisine in distinct:
                continue
            nodes += 1
            if nodes > _SEARCH_BUDGET:
                raise _OutOfBudget
            rest = rec(i + 1, used | {c.id}, cuisines + [c.cuisine], fish + c.is_fish)
            if rest is not None:
                return [c] + rest
        return None

    try:
        return rec(0, set(), [], 0)
    except _OutOfBudget:
        return None


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


def _choose_menu(cands: list[_Cand], evenings: list[date]) -> dict[date, _Cand]:
    """Fill as many evenings as the rules allow; drop the latest evenings first."""
    for k in range(len(evenings), 0, -1):
        for days in itertools.combinations(evenings, k):
            chosen = _pick(cands, list(days))
            if chosen:
                return dict(zip(days, _arrange(chosen, list(days), evenings)))
    return {}


# --- building dishes and the basket ------------------------------------------------------


async def _lines(client, recipe: Recipe, resolve) -> tuple[list[Line], list[str], list[str]]:
    lines, pantry, not_found = [], [], []
    for text, name in recipe.ingredients:
        kind, p = await resolve(text, name)
        if kind == "pantry":
            pantry.append(text)
            continue
        if p is None:
            not_found.append(text)
            continue
        amount = parse_ingredient(text, name or text)
        lines.append(Line(p, packs_needed(amount, parse_unit_size(p.unit_size)), text))
    return lines, pantry, not_found


def _resolver(client, bonus: list[Product], pantry: list[str], cache: dict):
    async def resolve(text: str, name: str):
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


def _finish(dishes: list[Dish], short_by: int) -> Plan:
    """Merge quantities per product, top up deals only for shared products, split savings."""
    dishes = sorted(dishes, key=lambda d: d.date)
    need: dict[int, int] = {}
    users: dict[int, set[int]] = {}
    products: dict[int, Product] = {}
    for i, d in enumerate(dishes):
        for ln in d.lines:
            products.setdefault(ln.product.id, ln.product)
            need[ln.product.id] = need.get(ln.product.id, 0) + ln.qty
            users.setdefault(ln.product.id, set()).add(i)
    basket = {
        pid: (qty_for_deal(products[pid], n) if len(users[pid]) > 1 else n)
        for pid, n in need.items()
    }
    total_saving = {pid: savings(products[pid], q) for pid, q in basket.items()}
    out = []
    for d in dishes:
        mine: dict[int, int] = {}
        for ln in d.lines:
            mine[ln.product.id] = mine.get(ln.product.id, 0) + ln.qty
        share = sum(total_saving[pid] * q / need[pid] for pid, q in mine.items() if need[pid])
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
    )


# --- public API -----------------------------------------------------------------------------


async def build_plan(client, bonus: list[Product], s: Settings) -> Plan:
    evenings = sorted(s.evenings)
    detail_budget = max(MAX_RECIPE_DETAILS - len(evenings), 0)
    cands, used = await _gather(client, bonus, s, set(), evenings, detail_budget)
    menu = _choose_menu(cands, evenings)
    budget = [MAX_RECIPE_DETAILS - used]  # what is left for scaling to the persons
    resolve = _resolver(client, bonus, s.pantry, {})
    dishes = [
        await _make_dish(client, c, day, s.persons.get(day, SCORE_SERVINGS), resolve, budget)
        for day, c in sorted(menu.items())
    ]
    return _finish(dishes, short_by=len(evenings) - len(dishes))


async def swap_dish(
    client, plan: Plan, day: date, bonus: list[Product], s: Settings, exclude: set[int]
) -> Plan:
    """Best other recipe for `day` under the same rules; the rest of the week stays."""
    old = next((d for d in plan.dishes if d.date == day), None)
    others = [d for d in plan.dishes if d.date != day]
    skip = set(exclude) | {d.recipe.id for d in others} | ({old.recipe.id} if old else set())
    cands, _ = await _gather(client, bonus, s, skip, [day], MAX_RECIPE_DETAILS - 1)
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
    persons = old.persons if old else s.persons.get(day, SCORE_SERVINGS)
    resolve = _resolver(client, bonus, s.pantry, {})
    dish = await _make_dish(client, best, day, persons, resolve)
    dishes = others + [dish]
    filled = {d.date for d in dishes}
    return _finish(dishes, short_by=sum(1 for e in s.evenings if e not in filled))


async def rescale_dish(client, plan: Plan, day: date, persons: int) -> Plan:
    """The same recipe for another number of persons; only this dish changes."""
    old = next((d for d in plan.dishes if d.date == day), None)
    if old is None or persons < 1:
        return plan
    recipe = await client.get_recipe(old.recipe.id, servings=persons)

    name_of = {t: n or t for t, n in old.recipe.ingredients}
    by_name = {name_of.get(ln.from_ingredient, ""): ln.product for ln in old.lines}
    pantry_names = {name_of.get(t, t) for t in old.pantry}
    missing_names = {name_of.get(t, t) for t in old.not_found}
    known_bonus = [p for p in plan.products.values() if p.is_bonus]

    async def resolve(text: str, name: str):
        key = name or text
        if key in pantry_names:
            return "pantry", None
        if key in by_name:
            return "product", by_name[key]
        if key in missing_names:
            return "product", None
        return "product", await choose_product(client, parse_ingredient(text, key), known_bonus)

    lines, pantry, not_found = await _lines(client, recipe, resolve)
    dish = dataclasses.replace(
        old, recipe=recipe, persons=persons, lines=lines, pantry=pantry, not_found=not_found
    )
    dishes = [dish if d.date == day else d for d in plan.dishes]
    return _finish(dishes, short_by=plan.short_by)
