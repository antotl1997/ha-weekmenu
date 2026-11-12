"""v1.1: the user's own recipes, with negative ids, served next to AH's.

`/data/recipes.json` (schema, durable): {"recipes": [recipe, ...]}. An own recipe has a
negative id so it never meets an AH id. `LocalFirst` wraps the AH client: a negative id
is answered from this store, everything else goes to AH. A file that cannot be read, or
one from a newer add-on, loads as empty; saving over a newer file raises SchemaTooNew
(like History.record). Callers write under `ctx.lock`.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from app.ahclient import AhApiError, Recipe
from app.durable import schema_of, too_new, write_json

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

    def _write(self, recipes: list[OwnRecipe]) -> None:
        write_json(self.path, {"recipes": [asdict(x) for x in recipes]}, RECIPES_SCHEMA)

    def save(self, r: OwnRecipe) -> OwnRecipe:
        """Store `r` (id 0: the next free negative id). Raises SchemaTooNew."""
        recipes = self.all()
        if r.id == 0:
            r = OwnRecipe(**{**asdict(r), "id": min((x.id for x in recipes), default=0) - 1})
        for i, x in enumerate(recipes):
            if x.id == r.id:
                recipes[i] = r
                break
        else:
            recipes.append(r)
        self._write(recipes)
        return r

    def remove(self, id: int) -> bool:
        recipes = self.all()
        kept = [r for r in recipes if r.id != id]
        if len(kept) == len(recipes):
            return False
        self._write(kept)
        return True


_NUM = r"\d+(?:[.,]\d+)?"
_LEAD = re.compile(rf"^(?:({_NUM})\s*-\s*({_NUM})|(\d+)/(\d+)|({_NUM}))")


def _fmt(x: float) -> str:
    return f"{round(x, 2):.2f}".rstrip("0").rstrip(".").replace(".", ",")


def _num(s: str) -> float:
    return float(s.replace(",", "."))


def scale_text(text: str, factor: float) -> str:
    """Scale only a leading quantity (3, 1,5, 1/2 or a range 2-3); other text is unchanged."""
    m = _LEAD.match(text)
    if not m:
        return text
    if m.group(1) is not None:
        new = f"{_fmt(_num(m.group(1)) * factor)}-{_fmt(_num(m.group(2)) * factor)}"
    elif m.group(3) is not None:
        if int(m.group(4)) == 0:
            return text
        new = _fmt(int(m.group(3)) / int(m.group(4)) * factor)
    else:
        new = _fmt(_num(m.group(5)) * factor)
    return new + text[m.end():]


def to_recipe(r: OwnRecipe, persons: int) -> Recipe:
    factor = persons / r.servings if r.servings else 1.0
    return Recipe(r.id, r.title, r.url or "", r.cook, None, None, ["hoofdgerecht"],
                  [r.kind] if r.kind else [], persons,
                  [(scale_text(t, factor), "") for t in r.ingredients], list(r.steps),
                  r.image)


class LocalFirst:
    """The AH client, except that own (negative) recipe ids are answered from the store."""

    def __init__(self, client, store: RecipeStore):
        self._client = client
        self._store = store

    async def get_recipe(self, id: int, servings: int):
        if id < 0:
            own = self._store.get(id)
            if own is None:
                raise AhApiError("eigen recept bestaat niet meer", 404)
            return to_recipe(own, servings)
        return await self._client.get_recipe(id, servings)

    def __getattr__(self, name):
        return getattr(self._client, name)
