"""Home Assistant via de Supervisor: helpers lezen, sensoren zetten, melding sturen."""

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass, field
from collections.abc import Iterable
from datetime import date, timedelta
from pathlib import Path

import httpx

from app.durable import SchemaTooNew, migrate, schema_of, too_new, write_json
from app.planner import Plan
from app.week import WEEKDAGEN, bestelling_zin
from app.writer import WriteResult

log = logging.getLogger(__name__)

BASE_URL = "http://supervisor/core/api"
TOTAL_PATH = Path("/data/totaal.json")
AWAY_PATH = Path("/data/niet_thuis.json")  # {weekday: persons before "niet thuis"}
# schema of totaal.json and niet_thuis.json; 0 is a file from before 0.6
TOTAL_SCHEMA = 1
AWAY_SCHEMA = 1


def migrate_total_v0_to_v1(data) -> dict:
    """0 -> 1: a bare number becomes {"total": x}; a dict keeps total and counted
    (a list of weeks stays a list: update_total migrates that from the ledger).
    Anything else was read as 0 and stays that way."""
    if isinstance(data, (int, float)) and not isinstance(data, bool):
        return {"schema": 1, "total": float(data)}
    if isinstance(data, dict):
        return {"schema": 1, **{k: v for k, v in data.items() if k != "schema"}}
    return {"schema": 1}


def migrate_away_v0_to_v1(data) -> dict:
    """0 -> 1: the same {weekday: persons}, with the version field."""
    if isinstance(data, dict):
        return {"schema": 1, **{k: v for k, v in data.items() if k != "schema"}}
    return {"schema": 1}


_TOTAL_STEPS = {0: migrate_total_v0_to_v1}
_AWAY_STEPS = {0: migrate_away_v0_to_v1}

# All helper entity IDs live here, and nowhere else.
HELPER_AAN = "input_boolean.weekmenu_aan"
HELPER_AVONDEN = "input_number.weekmenu_avonden"  # since 0.4: the number of cooking moments


def helper_persons() -> dict[str, str]:
    return {dag: f"input_number.weekmenu_personen_{dag}" for dag in WEEKDAGEN}


_ALLOWED_NUMBERS = {HELPER_AVONDEN, *helper_persons().values()}

# Who gets what (options notify_owner and notify_today, v0.8): the owner gets the menu,
# errors, the login and the order reminder; the "today" targets only "Vanavond".
DEFAULT_OWNER = "mobile_app_s_26_ultra_anto"
DEFAULT_TODAY = ["mobile_app_s24_van_maartje"]
_NOTIFY_NAME = re.compile(r"^[a-z0-9_]+$")


def notify_target(raw) -> str | None:
    """A notify service name as the option may hold it ("notify.x", "notify/x" or "x"),
    as the bare name; None for anything that is not a plain service name, so an option
    can never point the post at another Home Assistant path."""
    if not isinstance(raw, str):
        return None
    name = raw.strip().lower()
    for prefix in ("notify.", "notify/"):
        if name.startswith(prefix):
            name = name[len(prefix):]
    return name if _NOTIFY_NAME.match(name) else None

DEFAULT_AANTAL = 5
DEFAULT_PERSONEN = 2
MIN_PERSONEN, MAX_PERSONEN = 1, 8  # at home; 0 only through "niet thuis"

_ABBR = {
    "maandag": "ma", "dinsdag": "di", "woensdag": "wo", "donderdag": "do",
    "vrijdag": "vr", "zaterdag": "za", "zondag": "zo",
}
_UNUSABLE = {"unavailable", "unknown", "none", ""}


@dataclass
class RawSettings:
    enabled: bool
    aantal: int
    persons: dict[str, int]
    defaulted: list[str] = field(default_factory=list)

    @property
    def away(self) -> set[str]:
        """Weekdays with 0 persons: not at home, no dinner."""
        return {dag for dag, n in self.persons.items() if n == 0}


def clamp_kookmomenten(n: int, eat: int) -> int:
    """Cooking moments bounded to 1 .. the number of eat days (at least 1)."""
    return max(1, min(int(n), max(1, eat)))


def eat_count(persons: dict[str, int]) -> int:
    """Eat days in a menu week: the weekdays not at 0 persons."""
    return sum(1 for dag in WEEKDAGEN if persons.get(dag, DEFAULT_PERSONEN) != 0)


def format_eur(x: float) -> str:
    return "€ " + f"{x:.2f}".replace(".", ",")


def load_total_state(
    path: Path = TOTAL_PATH,
) -> tuple[float, dict[str, float] | set[str] | None]:
    """The total and what was counted in it per week.

    `counted` lives in the same file as the total, written in one atomic replace,
    so an amount can never be in the total without being recorded. It is a dict
    {ISO week: amount counted}. A `set` of ISO weeks: an older file with a list
    (amounts unknown); `None`: no file yet, or an older file without it (plain
    number or {"total": x}). The caller migrates both from the ledger.
    """
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return 0.0, None
    # a file from a newer add-on is read as it is (display only: writing it refuses)
    if schema_of(data) <= TOTAL_SCHEMA:
        data = migrate(data, TOTAL_SCHEMA, _TOTAL_STEPS)
    if not isinstance(data, dict):
        return 0.0, None
    try:
        total = float(data.get("total") or 0.0)
    except (ValueError, TypeError):
        total = 0.0
    counted = data.get("counted")
    if isinstance(counted, dict):
        out: dict[str, float] = {}
        for k, v in counted.items():
            try:
                out[str(k)] = float(v)
            except (ValueError, TypeError):
                out[str(k)] = 0.0
        return total, out
    if isinstance(counted, list):
        return total, {str(x) for x in counted}
    return total, None


def load_total(path: Path = TOTAL_PATH) -> float:
    return load_total_state(path)[0]


def _save_total(path: Path, total: float,
                counted: dict[str, float] | list[str] | None = None) -> None:
    path = Path(path)
    if counted is None:  # keep what is already there
        known = load_total_state(path)[1]
        counted = sorted(known) if isinstance(known, set) else known
    data: dict = {"total": total}
    if isinstance(counted, dict):
        data["counted"] = {k: counted[k] for k in sorted(counted)}
    elif counted is not None:
        data["counted"] = list(counted)
    write_json(path, data, TOTAL_SCHEMA)  # SchemaTooNew over a newer file


def no_cooking_moment(plan: Plan, start: date, away: Iterable[date]) -> list[date]:
    """Eat days of the week (not away) with neither a dish nor leftovers, and not
    emptied by "Gerecht weghalen": fewer cooking moments than needed, or a cook
    day the planner could not fill."""
    away = set(away)
    taken = ({d.date for d in plan.dishes}
             | {d.leftover_date for d in plan.dishes if d.leftover_date}
             | set(plan.removed_days))
    days = (start + timedelta(days=i) for i in range(7))
    return [d for d in days if d not in away and d not in taken]


def plan_view(plan: Plan, week_start: date, target: str,
              away: Iterable[date] = (), new_folder: bool = False) -> dict:
    """Attributes for sensor.weekmenu. `target` is "order" or "list"; `away`: the
    evenings with 0 persons, without food; `new_folder`: the menu is from an older
    folder and the week's own folder is out ("Menu bijwerken")."""
    dishes = []
    eaten = {d.date for d in plan.dishes} | {d.leftover_date for d in plan.dishes
                                             if d.leftover_date}
    for d in plan.dishes:
        if d.leftover_date:  # "do · restjes van <gerecht>": an entry of its own
            dishes.append({
                "datum": d.leftover_date.isoformat(),
                "dag": WEEKDAGEN[d.leftover_date.weekday()],
                "titel": f"Restjes van {d.recipe.title}",
                "restjes_van": d.recipe.title,
                "restjes_datum": d.date.isoformat(),
                "url": d.recipe.url,
                "image": d.recipe.image,
                "besparing": 0.0,
            })
        dishes.append({
            "datum": d.date.isoformat(),
            "dag": WEEKDAGEN[d.date.weekday()],
            "titel": d.recipe.title,
            "url": d.recipe.url,
            "image": d.recipe.image,
            "kooktijd": d.cook,
            "oven": d.oven,
            "wacht": d.wait,
            "keuken": d.cuisine,
            "personen": d.persons,
            "besparing": round(d.savings, 2),
        })
    dishes.sort(key=lambda x: x["datum"])
    away = set(away)
    return {
        "week": week_start.isoformat(),
        "bestelling": bestelling_zin(week_start),
        "doel": "bestelling" if target == "order" else "lijst",
        "niet_gevonden": list(plan.not_found),
        "niet_thuis": [d.isoformat() for d in sorted(away)
                       if d not in eaten and d not in plan.removed_days],
        "geen_gerecht": [d.isoformat() for d in plan.removed_days],  # "Gerecht weghalen"
        "geen_kookmoment": [d.isoformat() for d in no_cooking_moment(plan, week_start, away)],
        "dishes": dishes,
        "new_folder_available": new_folder,
    }


class Hass:
    def __init__(self, http: httpx.AsyncClient, total_path: Path = TOTAL_PATH,
                 away_path: Path | None = None, owner: str = DEFAULT_OWNER,
                 today: Iterable[str] | None = None):
        self._http = http
        self.owner = notify_target(owner) or DEFAULT_OWNER
        self.today = [t for t in (notify_target(x) for x in (
            DEFAULT_TODAY if today is None else today)) if t]
        self._total_path = Path(total_path)
        self._away_path = (Path(away_path) if away_path is not None
                           else self._total_path.with_name(AWAY_PATH.name))
        self._away_lock = asyncio.Lock()
        self._headers = {"Authorization": f"Bearer {os.environ.get('SUPERVISOR_TOKEN', '')}"}

    async def _get_state(self, entity_id: str) -> str | None:
        resp = await self._http.get(f"{BASE_URL}/states/{entity_id}", headers=self._headers)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        state = resp.json().get("state")
        if state is None or str(state).strip().lower() in _UNUSABLE:
            return None
        return str(state)

    async def _post(self, path: str, payload: dict) -> None:
        resp = await self._http.post(f"{BASE_URL}/{path}", json=payload, headers=self._headers)
        resp.raise_for_status()

    async def _int(self, entity_id: str, default: int, defaulted: list[str]) -> int:
        state = await self._get_state(entity_id)
        try:
            if state is not None:
                return int(float(state))
        except ValueError:
            pass
        defaulted.append(entity_id)
        return default

    async def read_settings(self) -> RawSettings:
        defaulted: list[str] = []
        aan = await self._get_state(HELPER_AAN)
        if aan is None:
            defaulted.append(HELPER_AAN)
        aantal = await self._int(HELPER_AVONDEN, DEFAULT_AANTAL, defaulted)
        persons = {
            dag: await self._int(eid, DEFAULT_PERSONEN, defaulted)
            for dag, eid in helper_persons().items()
        }
        # cooking moments: never more than eat days, out of bounds is bounded, not refused
        bounded = clamp_kookmomenten(aantal, eat_count(persons))
        if bounded != aantal and HELPER_AVONDEN not in defaulted:
            await self._write_back_kookmomenten(bounded)
        return RawSettings(aan == "on", bounded, persons, defaulted)

    async def _write_back_kookmomenten(self, value: int) -> None:
        """Put the helper back in bounds; best effort, never fails the caller."""
        try:
            await self._post("services/input_number/set_value",
                             {"entity_id": HELPER_AVONDEN, "value": value})
        except Exception as e:  # noqa: BLE001 - a read must not fail on this write
            log.warning("Home Assistant: kookmomenten niet teruggezet (%s)", type(e).__name__)

    async def _rebound_kookmomenten(self) -> None:
        """After a "niet thuis" tap: the cooking moments helper within 1 .. eat days."""
        try:
            missing: list[str] = []
            aantal = await self._int(HELPER_AVONDEN, DEFAULT_AANTAL, missing)
            if missing:
                return
            persons = {dag: await self._int(eid, DEFAULT_PERSONEN, [])
                       for dag, eid in helper_persons().items()}
        except Exception as e:  # noqa: BLE001
            log.warning("Home Assistant: kookmomenten niet gecontroleerd (%s)", type(e).__name__)
            return
        bounded = clamp_kookmomenten(aantal, eat_count(persons))
        if bounded != aantal:
            await self._write_back_kookmomenten(bounded)

    async def publish(self, plan_view: dict, week_savings: float) -> None:
        await self._post("states/sensor.weekmenu", {
            # the number of dishes cooked; leftover nights are entries, not dishes
            "state": sum(1 for d in plan_view["dishes"] if "restjes_van" not in d),
            "attributes": {
                "friendly_name": "Weekmenu",
                "week": plan_view["week"],
                "bestelling": plan_view["bestelling"],
                "doel": plan_view["doel"],
                "niet_gevonden": plan_view["niet_gevonden"],
                "niet_thuis": plan_view.get("niet_thuis", []),
                "geen_gerecht": plan_view.get("geen_gerecht", []),
                "geen_kookmoment": plan_view.get("geen_kookmoment", []),
                "dishes": plan_view["dishes"],
                "new_folder_available": plan_view.get("new_folder_available", False),
            },
        })
        await self._post("states/sensor.weekmenu_besparing_week", {
            "state": round(week_savings, 2),
            "attributes": {
                "friendly_name": "Weekmenu besparing week",
                "unit_of_measurement": "€",
                "device_class": "monetary",
            },
        })

    async def publish_total(self, total: float,
                            counted: dict[str, float] | None = None) -> None:
        """Write total (and the counted weeks) atomically to the file, then the sensor."""
        _save_total(self._total_path, total, counted)
        await self._post("states/sensor.weekmenu_besparing_totaal", {
            "state": round(total, 2),
            "attributes": {
                "friendly_name": "Weekmenu besparing totaal",
                "unit_of_measurement": "€",
                "device_class": "monetary",
                "state_class": "total_increasing",
            },
        })

    async def set_setting(self, entity_id: str, value: str | int | bool) -> str | int | bool:
        """Set a weekmenu helper; returns the value set. Cooking moments are bounded
        to 1 .. the eat days of the week first."""
        if entity_id == HELPER_AAN:
            action = "turn_on" if value else "turn_off"
            await self._post(f"services/input_boolean/{action}", {"entity_id": entity_id})
        elif entity_id in _ALLOWED_NUMBERS:
            if entity_id == HELPER_AVONDEN:
                persons = {dag: await self._int(eid, DEFAULT_PERSONEN, [])
                           for dag, eid in helper_persons().items()}
                value = clamp_kookmomenten(int(value), eat_count(persons))
            await self._post("services/input_number/set_value",
                             {"entity_id": entity_id, "value": value})
        else:
            raise ValueError(f"geen weekmenu-helper: {entity_id!r}")
        return value

    def _load_away_memory(self) -> dict[str, int]:
        try:
            data = json.loads(self._away_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            return {}
        if schema_of(data) <= AWAY_SCHEMA:
            data = migrate(data, AWAY_SCHEMA, _AWAY_STEPS)
        if not isinstance(data, dict):
            return {}
        out: dict[str, int] = {}
        for dag, n in data.items():
            if dag in WEEKDAGEN and isinstance(n, int) and not isinstance(n, bool)                     and MIN_PERSONEN <= n <= MAX_PERSONEN:
                out[dag] = n
        return out

    def _save_away_memory(self, memory: dict[str, int]) -> None:
        write_json(self._away_path, memory, AWAY_SCHEMA)  # SchemaTooNew over a newer file

    async def set_away(self, dag: str, away: bool) -> int:
        """Not eating at home on `dag`: persons go to 0 and the old number is
        remembered (written first, so a failed post loses nothing); turning it off
        puts the old number back (only if the day is at 0), or the default. Returns the persons now set."""
        if dag not in WEEKDAGEN:
            raise ValueError(f"geen weekdag: {dag!r}")
        entity = helper_persons()[dag]
        async with self._away_lock:
            if too_new(self._away_path, AWAY_SCHEMA):  # before any helper changes
                raise SchemaTooNew(self._away_path)
            memory = self._load_away_memory()
            if away:
                current = await self._int(entity, DEFAULT_PERSONEN, [])
                if current >= MIN_PERSONEN:  # already 0: keep what was remembered
                    memory[dag] = min(current, MAX_PERSONEN)
                    self._save_away_memory(memory)
                await self.set_setting(entity, 0)
                await self._rebound_kookmomenten()
                return 0
            current = await self._int(entity, 0, [])
            if current >= MIN_PERSONEN:  # not away: never overwrite a number set by hand
                return current
            value = memory.get(dag, DEFAULT_PERSONEN)
            await self.set_setting(entity, value)
            if dag in memory:
                del memory[dag]
                self._save_away_memory(memory)
            await self._rebound_kookmomenten()
            return value

    async def notify(self, title: str, message: str, url: str | None = None) -> None:
        """A push to the owner (notify_owner)."""
        await self.notify_to(self.owner, title, message, url)

    async def notify_to(self, target: str, title: str, message: str,
                        url: str | None = None) -> None:
        name = notify_target(target)
        if name is None:
            raise ValueError("geen notify-dienst")
        payload: dict = {"title": title, "message": message}
        if url:
            payload["data"] = {"clickAction": url}
        await self._post(f"services/notify/{name}", payload)


def _plural(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def format_message(
    plan: Plan, result: WriteResult, period_warning: str | None,
    staples: int = 0, start: date | None = None, away: Iterable[date] = (),
) -> tuple[str, str]:
    """The message when the menu is made. `staples`: suggestions in the app not added
    yet; `start`: the menu Wednesday, named in the first line; `away`: evenings
    not at home (0 persons); a day that still has a dish is not named."""
    away = list(away)
    title = f"Weekmenu klaar · {format_eur(plan.savings)} bespaard"
    lines: list[str] = []
    if start is not None:
        lines.append(bestelling_zin(start))
    if period_warning:
        lines.append(period_warning)
    days: list[tuple[date, str]] = []
    for d in plan.dishes:
        days.append((d.date, f"{_ABBR[WEEKDAGEN[d.date.weekday()]]} {d.recipe.title} "
                             f"({format_eur(d.savings)})"))
        if d.leftover_date:
            days.append((d.leftover_date, f"{_ABBR[WEEKDAGEN[d.leftover_date.weekday()]]} "
                                          f"restjes van {d.recipe.title}"))
    lines += [text for _, text in sorted(days, key=lambda x: x[0])]
    eaten = {day for day, _ in days}
    for d in sorted(away):
        if d in eaten or d in plan.removed_days:  # a removed dish: "geen gerecht" below
            continue
        lines.append(f"{_ABBR[WEEKDAGEN[d.weekday()]]} niet thuis")
    for d in plan.removed_days:
        lines.append(f"{_ABBR[WEEKDAGEN[d.weekday()]]} geen gerecht")
    if start is not None:
        for d in no_cooking_moment(plan, start, away):
            lines.append(f"{_ABBR[WEEKDAGEN[d.weekday()]]} geen kookmoment")
    if result.held:  # nothing written yet: no order or list for this week (Ruling 42)
        lines.append(result.held)
    else:
        lines.append("Alles staat in je bestelling" if result.target == "order"
                     else "Alles staat op Mijn lijst")
    if result.removed:
        where = "uit je bestelling" if result.target == "order" else "van je lijst"
        lines.append(f"{_plural(len(result.removed), 'product', 'producten')} {where} gehaald")
    if staples > 0:
        lines.append(f"Ook {_plural(staples, 'vaste boodschap', 'vaste boodschappen')} "
                     "in de bonus, kies in de app")
    if plan.not_found:
        lines.append("Niet gevonden: " + ", ".join(plan.not_found))
    if result.leftovers:
        lines.append("Over van het vorige menu: "
                     + ", ".join(f"{t} ({q}x)" for t, q in result.leftovers))
    if result.pending:
        lines.append("Onzeker of gelukt: " + ", ".join(t for t, _ in result.pending))
    if result.unknown:
        lines.append("Controleer in de AH-app: " + ", ".join(t for t, _ in result.unknown))
    if plan.short_by > 0:
        lines.append("Te weinig recepten voor " + _plural(plan.short_by, "avond", "avonden"))
    if plan.budget_exhausted:
        lines.append("Zoeken ingekort")
    return title, "\n".join(lines)
