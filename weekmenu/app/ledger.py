"""Log per menu week: what the add-on put on the AH list/order.

One JSON file per menu week, named after the Wednesday (`YYYY-MM-DD.json`).
The log, not the AH list, is the source of truth for "already added", so
things the user removed again are never re-added.
"""
import json
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Literal

from app.durable import SchemaTooNew, migrate, schema_of, write_json
from app.planner import Plan

# schema of a week log file; 0 is every log from before 0.6 (no "schema" field)
LEDGER_SCHEMA = 1


def migrate_weeklog_v0_to_v1(data: dict) -> dict:
    """0 -> 1: only the version field; from_dict already reads every older form."""
    return {**data, "schema": 1}


_LEDGER_STEPS = {0: migrate_weeklog_v0_to_v1}


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
    # ISO day -> the whole dish (planner.dish_to_dict) before the last swap, for an exact "Terug"
    previous_dish: dict[str, dict] = field(default_factory=dict)
    extras_added: dict[int, int] = field(default_factory=dict)
    extras_savings: float = 0.0
    # Losse aanbiedingen (tab Aanbiedingen): pid -> quantity written, and their saving.
    # Never lowered by opschonen, so kept apart from `added`.
    offers_added: dict[int, int] = field(default_factory=dict)
    offers_savings: float = 0.0
    # Vaste boodschappen in de bonus: suggestions (staples.Staple.to_dict) and why they are
    # missing, when the purchase history could not be read
    staples: list[dict] = field(default_factory=list)
    staples_error: str | None = None
    # Write-ahead record of a write in flight: pid -> {"before", "delta", "target", "order_id"}
    # plus "kind": "extra" for a staple (it settles into extras_added instead of added),
    # or "kind": "offer" with "savings" (a loose offer: offers_added and offers_savings),
    # or "kind": "reduce" with a negative (or 0) delta and "remove", the amount that
    # leaves `added` once the lowering arrived
    pending: dict[int, dict] = field(default_factory=dict)
    # pid -> delta of writes whose outcome can never be known (their order is gone, or a
    # lowering landed halfway): never written again that week. The sign tells which:
    # > 0 an addition, < 0 a lowering ("kind": "reduce").
    unknown: dict[int, int] = field(default_factory=dict)
    # set when the menu was made from the current folder because next week's was missing
    period_warning: str | None = None
    # a round finished: apply returned and the message was attempted. Until then the
    # schedule completes the week once (completion_tried), from the log, never blind.
    round_done: bool = False
    completion_tried: bool = False
    notified: bool = False  # the "klaar" message for this menu went out
    # where everything in `added` went: "list", "order:<id>", or "mixed" once a later
    # write went elsewhere. Only an unmixed, known destination is ever lowered.
    added_to: str | None = None
    # the start of the bonus period the menu was made with (None: a log from before 0.4)
    folder_period_start: date | None = None
    # the start of the menu week's own folder, seen after the menu was made with an
    # older one; with new_folder_notified the "De nieuwe folder is er" push went out
    new_folder: date | None = None
    new_folder_notified: bool = False
    # why the last menu write wrote nothing (Ruling 42): the active order is delivered
    # outside this menu week, or its delivery day is unknown. None once a write went out.
    held: str | None = None
    # Ruling 44: the one push for a held week the schedule has moved past
    held_notified: bool = False
    # the products that push named; held again with another set resets held_notified
    held_notified_items: list[int] = field(default_factory=list)
    # Ruling 43: pid -> what a lowering found already gone (the user took it off in the
    # AH app). Never added again that week: Terug, a swap, Nieuw menu or bijwerken
    # count it as if the add-on still had it out there.
    removed_by_user: dict[int, int] = field(default_factory=dict)
    # not stored: the file was written by a newer add-on. Shown, never written (v0.6).
    too_new: bool = field(default=False, compare=False, repr=False)

    @property
    def extras_total(self) -> float:
        """What staples and loose offers saved: on top of the menu's own saving."""
        return round(self.extras_savings + self.offers_savings, 2)

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
            "previous_dish": {k: dict(v) for k, v in self.previous_dish.items()},
            "extras_added": {str(k): v for k, v in self.extras_added.items()},
            "extras_savings": self.extras_savings,
            "offers_added": {str(k): v for k, v in self.offers_added.items()},
            "offers_savings": self.offers_savings,
            "staples": [dict(x) for x in self.staples],
            "staples_error": self.staples_error,
            "pending": {str(k): dict(v) for k, v in self.pending.items()},
            "unknown": {str(k): v for k, v in self.unknown.items()},
            "period_warning": self.period_warning,
            "round_done": self.round_done,
            "completion_tried": self.completion_tried,
            "notified": self.notified,
            "added_to": self.added_to,
            "folder_period_start": _iso(self.folder_period_start),
            "new_folder": _iso(self.new_folder),
            "new_folder_notified": self.new_folder_notified,
            "held": self.held,
            "held_notified": self.held_notified,
            "held_notified_items": list(self.held_notified_items),
            "removed_by_user": {str(k): v for k, v in self.removed_by_user.items()},
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
            previous_dish={k: dict(v) for k, v in (d.get("previous_dish") or {}).items()},
            extras_added={int(k): int(v) for k, v in (d.get("extras_added") or {}).items()},
            extras_savings=float(d.get("extras_savings") or 0.0),
            offers_added={int(k): int(v) for k, v in (d.get("offers_added") or {}).items()},
            offers_savings=float(d.get("offers_savings") or 0.0),
            staples=[dict(x) for x in d.get("staples") or []],
            staples_error=d.get("staples_error"),
            pending={int(k): dict(v) for k, v in (d.get("pending") or {}).items()},
            unknown={int(k): int(v) for k, v in (d.get("unknown") or {}).items()},
            period_warning=d.get("period_warning"),
            # a log from before this field: its round was handled by the old schedule
            round_done=bool(d.get("round_done", d.get("plan") is not None)),
            completion_tried=bool(d.get("completion_tried", False)),
            notified=bool(d.get("notified", False)),
            added_to=d.get("added_to"),
            folder_period_start=_date(d.get("folder_period_start")),
            new_folder=_date(d.get("new_folder")),
            new_folder_notified=bool(d.get("new_folder_notified", False)),
            held=d.get("held"),
            held_notified=bool(d.get("held_notified", False)),
            held_notified_items=[int(x) for x in d.get("held_notified_items") or []],
            removed_by_user={int(k): int(v)
                             for k, v in (d.get("removed_by_user") or {}).items()},
        )


def _iso(d: date | None) -> str | None:
    return d.isoformat() if d else None


def _date(s) -> date | None:
    return date.fromisoformat(s) if s else None


def new_folder_available(log: WeekLog) -> bool:
    """The menu week's own folder is out and the menu was made with an older one."""
    return (bool(log.plan) and log.new_folder is not None
            and (log.folder_period_start is None or log.folder_period_start < log.new_folder))


def leftovers(log: WeekLog, plan: Plan) -> dict[int, int]:
    """Added by the add-on according to the log, but not (any more) needed by `plan`.

    A product in `log.unknown` is left out: it is already reported as "check in the AH app".
    """
    out = {}
    for p, n in log.added.items():
        if p in log.unknown:
            continue
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
        if schema_of(data) > LEDGER_SCHEMA:
            # from a newer add-on: read what this code understands, for display only
            try:
                wlog = WeekLog.from_dict(data)
                if wlog.week != week:
                    raise ValueError("week")
            except Exception:
                wlog = WeekLog(week=week)
            wlog.too_new = True
            return wlog
        return WeekLog.from_dict(migrate(data, LEDGER_SCHEMA, _LEDGER_STEPS))

    def save(self, log: WeekLog) -> None:
        """Durably, and never over a log from a newer add-on (SchemaTooNew)."""
        path = self._path(log.week)
        if log.too_new:
            raise SchemaTooNew(path)
        write_json(path, log.to_dict(), LEDGER_SCHEMA)

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
