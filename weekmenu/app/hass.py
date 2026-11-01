"""Home Assistant via de Supervisor: helpers lezen, sensoren zetten, melding sturen."""

import json
import os
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import httpx

from app.durable import fsync_dir
from app.planner import Plan
from app.week import GEEN_OUDERAVOND, WEEKDAGEN
from app.writer import WriteResult

BASE_URL = "http://supervisor/core/api"
TOTAL_PATH = Path("/data/totaal.json")

# All helper entity IDs live here, and nowhere else.
HELPER_AAN = "input_boolean.weekmenu_aan"
HELPER_AVONDEN = "input_number.weekmenu_avonden"
HELPER_OUDERS = "input_select.weekmenu_ouders"


def helper_persons() -> dict[str, str]:
    return {dag: f"input_number.weekmenu_personen_{dag}" for dag in WEEKDAGEN}


_ALLOWED_NUMBERS = {HELPER_AVONDEN, *helper_persons().values()}

NOTIFY_SERVICE = "notify/mobile_app_s_26_ultra_anto"

DEFAULT_AANTAL = 5
DEFAULT_PERSONEN = 2

_ABBR = {
    "maandag": "ma", "dinsdag": "di", "woensdag": "wo", "donderdag": "do",
    "vrijdag": "vr", "zaterdag": "za", "zondag": "zo",
}
_UNUSABLE = {"unavailable", "unknown", "none", ""}


@dataclass
class RawSettings:
    enabled: bool
    aantal: int
    ouders: str
    persons: dict[str, int]
    defaulted: list[str] = field(default_factory=list)


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
    if isinstance(data, (int, float)) and not isinstance(data, bool):
        return float(data), None
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
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(str(path) + ".tmp")
    try:
        tmp.write_text(json.dumps(data), encoding="utf-8")
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    fsync_dir(path.parent)


def plan_view(plan: Plan, week_start: date, target: str, ouders: str) -> dict:
    """Attributes for sensor.weekmenu. `target` is "order" or "list"."""
    dishes = []
    for d in plan.dishes:
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
    return {
        "week": week_start.isoformat(),
        "doel": "bestelling" if target == "order" else "lijst",
        "niet_gevonden": list(plan.not_found),
        "ouders": ouders,
        "dishes": dishes,
    }


class Hass:
    def __init__(self, http: httpx.AsyncClient, total_path: Path = TOTAL_PATH):
        self._http = http
        self._total_path = Path(total_path)
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
        ouders = await self._get_state(HELPER_OUDERS)
        if ouders is None:
            defaulted.append(HELPER_OUDERS)
            ouders = GEEN_OUDERAVOND
        persons = {
            dag: await self._int(eid, DEFAULT_PERSONEN, defaulted)
            for dag, eid in helper_persons().items()
        }
        return RawSettings(aan == "on", aantal, ouders, persons, defaulted)

    async def publish(self, plan_view: dict, week_savings: float) -> None:
        await self._post("states/sensor.weekmenu", {
            "state": len(plan_view["dishes"]),
            "attributes": {
                "friendly_name": "Weekmenu",
                "week": plan_view["week"],
                "doel": plan_view["doel"],
                "niet_gevonden": plan_view["niet_gevonden"],
                "ouders": plan_view["ouders"],
                "dishes": plan_view["dishes"],
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

    async def set_setting(self, entity_id: str, value: str | int | bool) -> None:
        if entity_id == HELPER_AAN:
            action = "turn_on" if value else "turn_off"
            await self._post(f"services/input_boolean/{action}", {"entity_id": entity_id})
        elif entity_id == HELPER_OUDERS:
            await self._post("services/input_select/select_option",
                             {"entity_id": entity_id, "option": str(value)})
        elif entity_id in _ALLOWED_NUMBERS:
            await self._post("services/input_number/set_value",
                             {"entity_id": entity_id, "value": value})
        else:
            raise ValueError(f"geen weekmenu-helper: {entity_id!r}")

    async def notify(self, title: str, message: str, url: str | None = None) -> None:
        payload: dict = {"title": title, "message": message}
        if url:
            payload["data"] = {"clickAction": url}
        await self._post(f"services/{NOTIFY_SERVICE}", payload)


def _plural(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def format_message(
    plan: Plan, result: WriteResult, ouders: str, period_warning: str | None,
    staples: int = 0,
) -> tuple[str, str]:
    """The Sunday message. `staples`: suggestions in the app not added yet."""
    title = f"Weekmenu klaar · {format_eur(plan.savings)} bespaard"
    lines: list[str] = []
    if period_warning:
        lines.append(period_warning)
    for d in plan.dishes:
        lines.append(f"{_ABBR[WEEKDAGEN[d.date.weekday()]]} {d.recipe.title} ({format_eur(d.savings)})")
    if ouders == GEEN_OUDERAVOND:
        lines.append("deze week niet bij de ouders")
    else:
        lines.append(f"{_ABBR.get(ouders, ouders)} bij de ouders")
    lines.append("Alles staat in je bestelling" if result.target == "order"
                 else "Alles staat op Mijn lijst")
    if staples > 0:
        lines.append(f"Ook {_plural(staples, 'vaste boodschap', 'vaste boodschappen')} "
                     "in de bonus, kies in de app")
    if plan.not_found:
        lines.append("Niet gevonden: " + ", ".join(plan.not_found))
    if result.leftovers:
        lines.append("Restjes van het oude menu: "
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
