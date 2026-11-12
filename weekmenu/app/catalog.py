"""v1.1: the choice list. AH recipes scored against a folder, kept per folder period.

Scoring an AH recipe takes a get_recipe call (its ingredients), so the list cannot be
made when it opens. `fill` fetches and scores the recipes for the folder's bonus
products in the background, `search_ah` adds what the user searched for, and
`choose_view` only reads what is kept, with the own recipes scored locally.

`/data/catalog.json` (schema, durable): {"periods": {"<period start>": [entry, ...]}}.
At most KEEP periods, like the folder cache. A file that cannot be read, or one from a
newer add-on, loads as empty: the list is then simply filled again. Saving never stops
anything (logged). Callers of `fill` and `search_ah` hold `ctx.lock`.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import asdict, dataclass, fields
from datetime import date, timedelta
from pathlib import Path

from app.ahclient import AhApiError, Product, Recipe, RecipeSummary, is_blocking
from app.durable import schema_of, write_json
from app.planner import (
    CUISINES,
    DOWN_FACTOR,
    EXCLUDE_TITLES,
    MAIN_COURSE,
    MAX_COOK_WEEKDAY,
    SCORE_SERVINGS,
    _fold,
    cuisine_of,
    score_recipe,
    search_terms,
)
from app.recipes import to_recipe

log = logging.getLogger(__name__)

CATALOG_SCHEMA = 1
KEEP = 3


@dataclass
class Entry:
    id: int
    title: str
    image: str | None
    cook: int | None
    oven: int | None
    wait: int | None
    cuisine: str | None
    score: float
    is_fish: bool


_FIELDS = {f.name for f in fields(Entry)}


def _holding(keys, day: date) -> date | None:
    """The period start among `keys` whose week holds `day` (the newest of them)."""
    return max((k for k in keys if k <= day <= k + timedelta(days=6)), default=None)


class Catalog:
    """Scored AH recipes per folder period. `start` may be any day of the period."""

    def __init__(self, path: Path | None):
        self.path = Path(path) if path is not None else None
        self._periods: dict[date, dict[int, Entry]] = self._load() if self.path else {}

    def entries(self, start: date) -> list[Entry]:
        key = _holding(self._periods, start)
        return list(self._periods[key].values()) if key is not None else []

    def has(self, start: date) -> bool:
        return bool(self.entries(start))

    def periods(self) -> list[date]:
        return sorted(self._periods)

    def add(self, start: date, entries: list[Entry]) -> None:
        """Add to the period starting at `start` (one entry per id, the last one wins),
        keep the newest KEEP periods, and save when there is a file."""
        period = dict(self._periods.get(start, {}))
        for e in entries:
            period[e.id] = e
        out = {**self._periods, start: period}
        # one swap, so a reader in another thread sees the old or the new periods
        self._periods = {k: out[k] for k in sorted(out)[-KEEP:]}
        if self.path is not None:
            self._save()

    def _load(self) -> dict[date, dict[int, Entry]]:
        try:
            data = json.loads(self.path.read_bytes().decode("utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as e:
            log.warning("%s niet te lezen (%s)", self.path.name, type(e).__name__)
            return {}
        if not isinstance(data, dict) or schema_of(data) != CATALOG_SCHEMA:
            return {}  # a newer (or unknown) form: filled again
        periods = data.get("periods")
        out: dict[date, dict[int, Entry]] = {}
        for key, items in periods.items() if isinstance(periods, dict) else ():
            try:
                entries = [Entry(**{k: v for k, v in x.items() if k in _FIELDS})
                           for x in items]
                out[date.fromisoformat(key)] = {e.id: e for e in entries}
            except (TypeError, ValueError, AttributeError):
                log.warning("Keuzelijst van %s in %s overgeslagen", key, self.path.name)
        return {k: out[k] for k in sorted(out)[-KEEP:]}

    def _save(self) -> None:
        try:
            write_json(self.path, {"periods": {
                k.isoformat(): [asdict(e) for e in v.values()]
                for k, v in sorted(self._periods.items())}}, CATALOG_SCHEMA)
        except Exception as e:  # a lost list only costs filling it again
            log.warning("%s niet te bewaren (%s)", self.path.name, type(e).__name__)


async def _entry(summ: RecipeSummary, term: str, recipe: Recipe, bonus: list[Product],
                 pantry: list[str], overrides: dict) -> Entry:
    score, cuisine, fish = await score_recipe(recipe, bonus, pantry, overrides, term)
    return Entry(
        id=recipe.id, title=recipe.title, image=recipe.image or summ.image,
        cook=recipe.cook if recipe.cook is not None else summ.cook,
        oven=recipe.oven if recipe.oven is not None else summ.oven,
        wait=recipe.wait if recipe.wait is not None else summ.wait,
        cuisine=cuisine, score=score, is_fish=fish,
    )


async def _fetch(ctx, wanted: list[tuple[str, RecipeSummary]], bonus: list[Product],
                 period: date, limit: int) -> list[Entry]:
    """get_recipe and score the first `limit` of `wanted` (search term, summary), and
    add them to `period`, the period of the folder `bonus` they are scored against.
    What was fetched before a blocking AH error is kept; the error goes on."""
    pantry, overrides = list(ctx.pantry), ctx.overrides.load()
    new: list[Entry] = []
    try:
        for term, summ in wanted[:limit]:
            try:
                recipe = await ctx.client.get_recipe(summ.id, servings=SCORE_SERVINGS)
            except AhApiError as e:
                if is_blocking(e):
                    raise
                continue
            new.append(await _entry(summ, term, recipe, bonus, pantry, overrides))
    finally:
        if new:
            await asyncio.to_thread(ctx.catalog.add, period, new)
    return new


async def _search(ctx, text: str) -> list[RecipeSummary]:
    """Main courses AH finds for `text`; a failure that does not block is no hits."""
    try:
        found = await ctx.client.search_recipes(text)
    except AhApiError as e:
        if is_blocking(e):
            raise
        return []
    return [r for r in found if MAIN_COURSE in (r.courses or [])]


async def fill(ctx, start: date, budget: int = 60) -> int:
    """Fetch and score up to `budget` new recipes for the bonus of the folder of the menu
    week `start`. Returns how many were added. Caller holds `ctx.lock`."""
    from app import main

    got = await main.folder_for_week(ctx, start, ctx.clock(), fallback=True)
    bonus, _warning, period = got  # fallback=True never gives Waiting
    known = {e.id for e in ctx.catalog.entries(period)}
    never = ctx.taste.never()
    lists = [(term, await _search(ctx, term)) for term in search_terms(bonus)]
    # round robin over the searches, so the best product does not take every call
    seen: set[int] = set()
    wanted: list[tuple[str, RecipeSummary]] = []
    for i in range(max((len(x) for _, x in lists), default=0)):
        for term, found in lists:
            if i >= len(found):
                continue
            r = found[i]
            if r.id in seen or r.id in known or r.id in never:
                continue
            seen.add(r.id)
            if any(x in _fold(r.title) for x in EXCLUDE_TITLES):
                continue
            wanted.append((term, r))
    return len(await _fetch(ctx, wanted, bonus, period, budget))


async def search_ah(ctx, start: date, q: str, limit: int = 12) -> list[Entry]:
    """Search AH for `q`, fetch and score the first `limit` main courses not known yet,
    add them to the folder period of `start`, and return the matches (known and new) in
    AH's order. Caller holds `ctx.lock`."""
    from app import main

    got = await main.folder_for_week(ctx, start, ctx.clock(), fallback=True)
    bonus, _warning, period = got
    found = await _search(ctx, q)
    known = {e.id: e for e in ctx.catalog.entries(period)}
    never = ctx.taste.never()
    wanted = [(q, r) for r in found if r.id not in known and r.id not in never]
    for e in await _fetch(ctx, wanted, bonus, period, limit):
        known[e.id] = e
    return [known[r.id] for r in found if r.id in known]


def period_for(ctx, start: date) -> date | None:
    """The folder period the choice list of the menu week `start` uses, without AH calls:
    its own period when its folder or list is there; else the current folder standing in
    for it (offers_fallback, or the newest period starting at most 13 days before)."""
    keys = set(ctx.bonus_cache) | set(ctx.catalog.periods())
    own = _holding(keys, start)
    if own is not None:
        return own
    known = ctx.offers_fallback.get(start)
    if known is not None and known[0] in keys:
        return known[0]
    return max((k for k in keys if start - timedelta(days=13) <= k <= start), default=None)


def item(id, title, image, cook, cuisine, score, source, day, weeks_ago) -> dict:
    return {
        "id": id, "title": title, "image": image, "cook": cook, "cuisine": cuisine,
        "savings": round(score, 2), "source": source,
        "long": day is not None and day.weekday() < 5 and (cook or 0) > MAX_COOK_WEEKDAY,
        "eaten_weeks_ago": weeks_ago.get(id),
    }


async def choose_view(ctx, start: date, day: date | None, q: str = "",
                      kind: str = "") -> dict:
    """The choice list for the menu week `start` (and the evening `day`, for the cooking
    time mark): kept AH recipes and own recipes, best saving first. No AH call."""
    never, down = ctx.taste.never(), ctx.taste.down()
    weeks_ago = ctx.ledger.recent_recipe_weeks(start)
    period = period_for(ctx, start)
    entries = ctx.catalog.entries(period) if period is not None else []
    items = [
        item(e.id, e.title, e.image, e.cook, e.cuisine, e.score, "ah", day, weeks_ago)
        for e in entries
    ]
    folder = ctx.bonus_cache.get(period) or []
    pantry, overrides = list(ctx.pantry), ctx.overrides.load()
    for r in ctx.recipes.all():
        recipe = to_recipe(r, r.servings)
        if folder:
            score, cuisine, _fish = await score_recipe(recipe, folder, pantry, overrides)
        else:
            score, cuisine = 0.0, cuisine_of(recipe)
        items.append(item(r.id, r.title, r.image, r.cook, r.kind or cuisine, score,
                           r.source or "eigen", day, weeks_ago))
    needle = q.strip().casefold()
    out = []
    for x in items:
        if x["id"] in never:
            continue
        if needle and needle not in x["title"].casefold():
            continue
        if kind and x["cuisine"] != kind:
            continue
        if x["id"] in down:  # duim omlaag: lower in the list, still there
            x["savings"] = round(x["savings"] * DOWN_FACTOR, 2)
        out.append(x)
    out.sort(key=lambda x: (-x["savings"], x["title"].casefold()))
    return {"items": out, "kinds": [k["naam"] for k in CUISINES],
            "catalog_ready": bool(entries)}
