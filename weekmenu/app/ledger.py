"""Log per menu week: what the add-on put on the AH list/order.

One JSON file per menu week, named after the Wednesday (`YYYY-MM-DD.json`).
The log, not the AH list, is the source of truth for "already added", so
things the user removed again are never re-added.
"""
import json
import os
import time
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Literal

from app.planner import Plan


@dataclass
class WeekLog:
    week: date
    plan: dict | None = None  # Plan.to_dict()
    added: dict[int, int] = field(default_factory=dict)
    target: Literal["order", "list"] | None = None
    savings: float = 0.0
    counted: bool = False
    swapped_out: dict[str, list[int]] = field(default_factory=dict)  # ISO day -> recipe ids
    previous: dict[str, int] = field(default_factory=dict)  # ISO day -> recipe id before last swap
    extras_added: dict[int, int] = field(default_factory=dict)
    extras_savings: float = 0.0
    # Write-ahead record of a write in flight: pid -> {"before", "delta", "target", "order_id"}
    pending: dict[int, dict] = field(default_factory=dict)
    # pid -> delta of writes whose outcome can never be known (their order is gone): never written again
    unknown: dict[int, int] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "week": self.week.isoformat(),
            "plan": self.plan,
            "added": {str(k): v for k, v in self.added.items()},
            "target": self.target,
            "savings": self.savings,
            "counted": self.counted,
            "swapped_out": {k: list(v) for k, v in self.swapped_out.items()},
            "previous": dict(self.previous),
            "extras_added": {str(k): v for k, v in self.extras_added.items()},
            "extras_savings": self.extras_savings,
            "pending": {str(k): dict(v) for k, v in self.pending.items()},
            "unknown": {str(k): v for k, v in self.unknown.items()},
        }

    @classmethod
    def from_dict(cls, d: dict) -> "WeekLog":
        return cls(
            week=date.fromisoformat(d["week"]),
            plan=d.get("plan"),
            added={int(k): int(v) for k, v in (d.get("added") or {}).items()},
            target=d.get("target"),
            savings=float(d.get("savings") or 0.0),
            counted=bool(d.get("counted", False)),
            swapped_out={k: [int(i) for i in v] for k, v in (d.get("swapped_out") or {}).items()},
            previous={k: int(v) for k, v in (d.get("previous") or {}).items()},
            extras_added={int(k): int(v) for k, v in (d.get("extras_added") or {}).items()},
            extras_savings=float(d.get("extras_savings") or 0.0),
            pending={int(k): dict(v) for k, v in (d.get("pending") or {}).items()},
            unknown={int(k): int(v) for k, v in (d.get("unknown") or {}).items()},
        )


def leftovers(log: WeekLog, plan: Plan) -> dict[int, int]:
    """Added by the add-on according to the log, but not (any more) needed by `plan`."""
    out = {}
    for p, n in log.added.items():
        extra = n - plan.basket.get(p, 0)
        if extra > 0:
            out[p] = extra
    return out


class Ledger:
    def __init__(self, root: Path):
        self.root = Path(root)

    def _path(self, week: date) -> Path:
        return self.root / f"{week.isoformat()}.json"

    def load(self, week: date) -> WeekLog:
        try:
            data = json.loads(self._path(week).read_text(encoding="utf-8"))
        except FileNotFoundError:
            return WeekLog(week=week)
        return WeekLog.from_dict(data)

    def save(self, log: WeekLog) -> None:
        path = self._path(log.week)
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = Path(str(path) + ".tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(json.dumps(log.to_dict()))
                f.flush()
                os.fsync(f.fileno())
            for attempt in range(6):
                try:
                    os.replace(tmp, path)
                    break
                except PermissionError:  # Windows: a scanner may hold the file for a moment
                    if attempt == 5:
                        raise
                    time.sleep(0.05)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise

    def _all(self) -> list[WeekLog]:
        logs = []
        for p in sorted(self.root.glob("*.json")) if self.root.exists() else []:
            try:
                logs.append(self.load(date.fromisoformat(p.stem)))
            except ValueError:
                continue
        return logs

    def weeks(self) -> list[WeekLog]:
        """Every week log on disk, oldest first."""
        return self._all()

    def recent_recipe_ids(self, before: date, weeks: int = 4) -> set[int]:
        ids: set[int] = set()
        for i in range(1, weeks + 1):
            log = self.load(before - timedelta(days=7 * i))
            for d in (log.plan or {}).get("dishes") or []:
                ids.add(int(d["recipe"]["id"]))
        return ids

    def weeks_to_count(self, today: date) -> list[WeekLog]:
        return [lg for lg in self._all() if lg.week <= today and not lg.counted]
