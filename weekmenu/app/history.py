"""v0.9: which recipes were swapped out or removed, per menu week, without asking why.

`/data/history.json` (schema, durable): {"weeks": {"<menu week>": [event, ...]}} with
per event the evening, the recipe, what happened ("swap": "Ander gerecht", "back":
"Terug" pushed the replacement out, "remove": "Gerecht weghalen") and when. No screen
uses it yet; 1.0 counts its numbers from it. A failure to write it never stops the
change itself (logged). Callers write under `ctx.lock`.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime
from pathlib import Path

from app.durable import too_new, write_json

log = logging.getLogger(__name__)

HISTORY_SCHEMA = 1
ACTIONS = ("swap", "back", "remove")


class History:
    def __init__(self, path: Path):
        self.path = Path(path)

    def load(self) -> dict[str, list[dict]]:
        try:
            data = json.loads(self.path.read_bytes().decode("utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as e:
            log.warning("%s niet te lezen (%s)", self.path.name, type(e).__name__)
            return {}
        weeks = data.get("weeks") if isinstance(data, dict) else None
        if not isinstance(weeks, dict):
            return {}
        return {k: [e for e in v if isinstance(e, dict)]
                for k, v in weeks.items() if isinstance(v, list)}

    def too_new(self) -> bool:
        return too_new(self.path, HISTORY_SCHEMA)

    def record(self, week: date, day: date, recipe_id: int, title: str, action: str,
               at: datetime) -> None:
        """Add one event. Raises SchemaTooNew for a newer add-on's file (nothing written)."""
        if action not in ACTIONS:
            raise ValueError(action)
        weeks = self.load()
        weeks.setdefault(week.isoformat(), []).append({
            "date": day.isoformat(), "recipe_id": int(recipe_id), "title": title,
            "action": action, "at": at.isoformat(timespec="minutes"),
        })
        write_json(self.path, {"weeks": weeks}, HISTORY_SCHEMA)
