"""v1.1: the user's own recipes, with negative ids, served next to AH's.

`/data/recipes.json` (schema, durable): {"recipes": [recipe, ...]}. An own recipe has a
negative id so it never meets an AH id. `LocalFirst` wraps the AH client: a negative id
is answered from this store, everything else goes to AH. A file that cannot be read, or
one from a newer add-on, loads as empty; saving over a newer file raises SchemaTooNew
(like History.record), and an unreadable file is set aside before the first save. Callers write under `ctx.lock`.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass, field, fields, replace
from datetime import datetime
from pathlib import Path

from app.ahclient import AhApiError, Recipe
from app.durable import schema_of, too_new, write_json
from app.ingredients import ingredient_name

log = logging.getLogger(__name__)

RECIPES_SCHEMA = 1


@dataclass
class OwnRecipe:
    id: int
    title: str
    servings: int
    ingredients: list[str] = field(default_factory=list)
    steps: list[str] = field(default_factory=list)
    cook: int | None = None
    kind: str | None = None  # a keuken name from keukens.yaml, or None
    source: str | None = None  # site name, None for "eigen"
    url: str | None = None
    image: str | None = None
    updated: str = ""  # ISO, to the minute


_FIELDS = {f.name for f in fields(OwnRecipe)}


class RecipeStore:
    def __init__(self, path: Path):
        self.path = Path(path)

    def all(self) -> list[OwnRecipe]:
        try:
            data = json.loads(self.path.read_bytes().decode("utf-8"))
        except FileNotFoundError:
            return []
        except (OSError, ValueError) as e:
            log.warning("%s niet te lezen (%s)", self.path.name, type(e).__name__)
            return []
        if not isinstance(data, dict) or schema_of(data) > RECIPES_SCHEMA:
            return []  # a newer form: shown as empty, never written
        out: list[OwnRecipe] = []
        items = data.get("recipes")
        for item in items if isinstance(items, list) else ():
            try:
                out.append(OwnRecipe(**{k: v for k, v in item.items() if k in _FIELDS}))
            except (TypeError, AttributeError):
                log.warning("Eigen recept overgeslagen in %s", self.path.name)
        return out

    def get(self, id: int) -> OwnRecipe | None:
        return next((r for r in self.all() if r.id == id), None)

    def too_new(self) -> bool:
        return too_new(self.path, RECIPES_SCHEMA)

    def _next_id(self, recipes: list[OwnRecipe]) -> int:
        """The next id to hand out: never one that was used before (taste, history and
        plans are keyed on the id). Kept in the file; derived from the lowest id when
        the file has none."""
        derived = min((x.id for x in recipes), default=0) - 1
        try:
            data = json.loads(self.path.read_bytes().decode("utf-8"))
            stored = data.get("next_id") if isinstance(data, dict) else None
        except (OSError, ValueError):
            stored = None
        if isinstance(stored, int) and not isinstance(stored, bool) and stored < 0:
            return min(stored, derived)
        return derived

    def _set_aside_broken(self) -> None:
        """A file that exists but cannot be read as recipes (loaded as empty) is renamed
        to recipes.json.broken-<time> before the first write, so a save never overwrites
        the user's recipes. A failing rename is raised: then nothing is written."""
        try:
            data = json.loads(self.path.read_bytes().decode("utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError):
            data = None
        if isinstance(data, dict) and isinstance(data.get("recipes", []), list):
            return  # readable (a newer form is refused by write_json itself)
        aside = self.path.with_name(
            f"{self.path.name}.broken-{datetime.now().strftime('%Y%m%d-%H%M%S')}")
        self.path.rename(aside)
        log.warning("%s was niet te lezen; bewaard als %s, er komt een nieuw bestand",
                    self.path.name, aside.name)

    def _write(self, recipes: list[OwnRecipe], next_id: int) -> None:
        self._set_aside_broken()
        write_json(self.path, {"recipes": [asdict(x) for x in recipes],
                               "next_id": next_id}, RECIPES_SCHEMA)

    def save(self, r: OwnRecipe) -> OwnRecipe:
        """Store `r` (id 0: a new, never used negative id). Raises SchemaTooNew;
        ValueError for a positive id (those are AH's)."""
        if r.id > 0:
            raise ValueError("eigen recepten hebben een negatief id")
        recipes = self.all()
        next_id = self._next_id(recipes)
        if r.id == 0:
            r = OwnRecipe(**{**asdict(r), "id": next_id})
        for i, x in enumerate(recipes):
            if x.id == r.id:
                recipes[i] = r
                break
        else:
            recipes.append(r)
        self._write(recipes, min(next_id, r.id - 1))
        return r

    def remove(self, id: int) -> bool:
        recipes = self.all()
        kept = [r for r in recipes if r.id != id]
        if len(kept) == len(recipes):
            return False
        self._write(kept, self._next_id(recipes))
        return True


_NUM = r"\d+(?:[.,]\d+)?"
_LEAD = re.compile(rf"^(?:(\d+)\s+(\d+)/(\d+)|({_NUM})\s*-\s*({_NUM})|(\d+)/(\d+)|({_NUM}))")


def _fmt(x: float) -> str:
    return f"{round(x, 2):.2f}".rstrip("0").rstrip(".").replace(".", ",")


def _num(s: str) -> float:
    return float(s.replace(",", "."))


def scale_text(text: str, factor: float) -> str:
    """Scale only a leading quantity (3, 1,5, 1/2 or a range 2-3); other text is unchanged."""
    m = _LEAD.match(text)
    if not m:
        return text
    if m.group(1) is not None:  # mixed number: 1 1/2
        if int(m.group(3)) == 0:
            return text
        new = _fmt((int(m.group(1)) + int(m.group(2)) / int(m.group(3))) * factor)
    elif m.group(4) is not None:
        new = f"{_fmt(_num(m.group(4)) * factor)}-{_fmt(_num(m.group(5)) * factor)}"
    elif m.group(6) is not None:
        if int(m.group(7)) == 0:
            return text
        new = _fmt(int(m.group(6)) / int(m.group(7)) * factor)
    else:
        new = _fmt(_num(m.group(8)) * factor)
    return new + text[m.end():]


def to_recipe(r: OwnRecipe, persons: int) -> Recipe:
    factor = persons / r.servings if r.servings else 1.0
    return Recipe(r.id, r.title, r.url or "", r.cook, None, None, ["hoofdgerecht"],
                  [r.kind] if r.kind else [], persons,
                  [(scale_text(t, factor), ingredient_name(t)) for t in r.ingredients],
                  list(r.steps),
                  r.image)


def is_own(recipe_id: int) -> bool:
    """Own recipes have negative ids; AH's are positive."""
    return recipe_id < 0


def scale_recipe(recipe: Recipe, persons: int) -> Recipe:
    """An already made recipe (an own dish's copy) for another number of persons."""
    factor = persons / recipe.servings if recipe.servings else 1.0
    return replace(recipe, servings=persons,
                   ingredients=[(scale_text(t, factor), n) for t, n in recipe.ingredients])


class LocalFirst:
    """The AH client, except that own (negative) recipe ids are answered from the store."""

    def __init__(self, client, store: RecipeStore):
        self._client = client
        self._store = store

    async def get_recipe(self, id: int, servings: int):
        if is_own(id):
            own = self._store.get(id)
            if own is None:
                raise AhApiError("eigen recept bestaat niet meer", 404)
            return to_recipe(own, servings)
        return await self._client.get_recipe(id, servings)

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self._client, name)
