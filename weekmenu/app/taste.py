"""v1.0: duim omlaag and "nooit meer" per dish, kept in `/data/taste.json`.

{"schema": 1, "recipes": {"<recipe id>": {"kind": "down" | "never", "title", "at"}}}

- "down" (duim omlaag): a soft penalty, the recipe's score in the ranking times
  DOWN_FACTOR (min 25 procent); it can still be chosen.
- "never" (nooit meer): the planner leaves the recipe out, also for Ander gerecht,
  until it is taken back in Instellingen ("Nooit meer").

One choice per recipe: "never" replaces a thumb. Written with the schema and
`durable.write_json` like every other file; a file from a newer add-on is read but never
written (SchemaTooNew). Callers write under `ctx.lock`. Nothing here goes to AH.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

from app.durable import too_new, write_json

log = logging.getLogger(__name__)

TASTE_SCHEMA = 1
DOWN = "down"
NEVER = "never"
KINDS = (DOWN, NEVER)
DOWN_FACTOR = 0.75  # duim omlaag: score min 25 procent


class Taste:
    def __init__(self, path: Path):
        self.path = Path(path)

    def load(self) -> dict[int, dict]:
        """recipe id -> choice. A missing or unreadable file is no choices (logged)."""
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
            log.warning("%s is beschadigd: smaak telt niet mee", self.path.name)
            return {}
        recipes = data.get("recipes") if isinstance(data, dict) else None
        if not isinstance(recipes, dict):
            return {}
        out = {}
        for k, v in recipes.items():
            if isinstance(k, str) and k.isdigit() and isinstance(v, dict) \
                    and v.get("kind") in KINDS:
                out[int(k)] = v
        return out

    def too_new(self) -> bool:
        return too_new(self.path, TASTE_SCHEMA)

    def never(self) -> set[int]:
        return {k for k, v in self.load().items() if v["kind"] == NEVER}

    def down(self) -> set[int]:
        return {k for k, v in self.load().items() if v["kind"] == DOWN}

    def kind_of(self, recipe_id: int) -> str | None:
        e = self.load().get(int(recipe_id))
        return e["kind"] if e else None

    def _write(self, items: dict[int, dict]) -> None:
        write_json(self.path, {"recipes": {str(k): v for k, v in items.items()}},
                   TASTE_SCHEMA)

    def put(self, recipe_id: int, kind: str, title: str, at: datetime) -> None:
        """Set the choice for one recipe (SchemaTooNew from a newer add-on's file)."""
        if kind not in KINDS:
            raise ValueError(kind)
        items = self.load()
        items[int(recipe_id)] = {"kind": kind, "title": str(title or ""),
                                 "at": at.isoformat(timespec="minutes")}
        self._write(items)

    def remove(self, recipe_id: int) -> bool:
        """Take the choice back; False when there was none."""
        items = self.load()
        if int(recipe_id) not in items:
            return False
        del items[int(recipe_id)]
        self._write(items)
        return True

    def listing(self) -> list[dict]:
        """For "Nooit meer" in Instellingen: only those, newest first."""
        out = [{"recipe_id": k, "title": v.get("title") or str(k), "kind": v["kind"],
                "at": v.get("at")}
               for k, v in self.load().items() if v["kind"] == NEVER]
        out.sort(key=lambda x: (x["at"] or "", x["recipe_id"]), reverse=True)
        return out
