"""The add-on process: one round tied together, the schedule, stdin commands.

A round: read the helpers, take the bonus period of the menu Wednesday, build the
whole menu (or reuse the week's menu from the log), then write the delta to AH,
publish the sensors and send the push message.

The schedule: every day from `schedule_time` to 18:00, once an hour, as long as the
log has no menu for the next menu week, look whether its bonus period is out
(`bonus_periods`, nothing more) and make the menu when it is. Only on the Sunday
before that Wednesday, at 18:00, fall back to the current folder. The plan in the
log is the source of truth for "done"; state.json only remembers the last hour
looked at and when the folder is expected.

The buttons (Nu maken, Nieuw menu, stdin) always make a menu: before the folder is out
with the current one, and the warning. Once a menu exists the schedule never changes
it; when it was made with an older folder, the hourly look only tells, once, that the
week's own folder is out (`check_new_folder`). "Menu bijwerken" then remakes it.

ApiHealth (v0.7): two failures in a row on a critical AH endpoint pause every write
(the writer checks it). One push says so (`sync_health`), marked in state.json so it
goes once per pause, also over a restart; the pause itself is kept there too. While
paused the hourly look does a read round (`probe_health`) that can switch writing on
again; "Opnieuw proberen" in the app does it by hand.

Every path that writes to AH (the rounds and the write calls of the web API) runs
under `Context.lock`: two writers at once could
add a product twice. Calls to Home Assistant never break a round: their
`httpx.HTTPError` is logged and dropped.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
import time as _time
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Callable, Literal
from zoneinfo import ZoneInfo

import httpx

from app.ahclient import (
    APP_VERSION,
    AhApiError,
    AhClient,
    AhLoginExpired,
    AhOrderMode,
    BonusPeriod,
    Product,
)
from app.health import PAUSED_MESSAGE, ApiHealth
from app.hass import (
    AWAY_SCHEMA,
    DEFAULT_OWNER,
    DEFAULT_TODAY,
    TOTAL_PATH,
    TOTAL_SCHEMA,
    Hass,
    RawSettings,
    format_message,
    load_total_state,
    notify_target,
    plan_view,
    vanavond,
)
from app.foldercache import load_folders, remember, save_folders
from app.history import HISTORY_SCHEMA, History
from app.ledger import Ledger, WeekLog, new_folder_available
from app.overrides import OVERRIDES_SCHEMA, Overrides
from app.taste import TASTE_SCHEMA, Taste
from app import reconcile
from app.reconcile import RECON_SCHEMA, Reconciliations
from app.planner import (
    Plan,
    Settings,
    apply_override,
    build_plan,
    reresolve,
    dish_from_dict,
    dish_to_dict,
    put_dish,
    remove_dish,
    rescale_dish,
    restore_dish,
    swap_dish,
)
from app import staples
from app.durable import UPDATE_MESSAGE, SchemaTooNew, migrate, schema_of, too_new, write_json
from app.tokens import TokenStore
from app.week import (
    WEEKDAGEN,
    away_dates,
    eat_days,
    menu_week_start,
    plan_days,
)
from app import writer
from app.writer import WriteResult, apply

# swap_dish, rescale_dish, restore_dish and remove_dish are looked up here by the web API
__all__ = ["run_round", "swap_dish", "rescale_dish", "restore_dish", "put_dish", "remove_dish",
           "build_plan", "dish_to_dict", "dish_from_dict", "apply_override",
           "reresolve"]

log = logging.getLogger("weekmenu")

Mode = Literal["schema", "nu_maken", "nieuw_menu", "bijwerken"]
MODES = ("schema", "nu_maken", "nieuw_menu", "bijwerken")
BUTTONS = ("nu_maken", "nieuw_menu", "bijwerken")
REBUILD = ("nieuw_menu", "bijwerken")  # throw the week's menu away and choose again

DATA = Path("/data")
OPTIONS_PATH = DATA / "options.json"
DEFAULT_PANTRY = ["olie", "zout", "peper", "suiker", "bloem", "azijn"]
DEFAULT_SCHEDULE = time(8, 0)
DEFAULT_DINNER = time(18, 30)
LAST_CHECK = time(18, 0)  # the last look of the day; on the Sunday the fallback
LAST_FALLBACK_TRY = time(23, 0)  # on the fallback Sunday: keep trying a failing round
FALLBACK_DAYS = 3  # the fallback is on the Sunday, three days before the menu Wednesday
PERIOD_WARNING = (
    "De folder van volgende week was er nog niet: "
    "prijzen gelden woensdag waarschijnlijk niet meer"
)
NEW_FOLDER = "De nieuwe folder is er. Open de Weekmenu-app om je menu bij te werken."
LOGIN_EXPIRED = "AH-login verlopen, log opnieuw in"
LOGIN_EXPIRED_COMPLETION = ("AH-login verlopen. Log opnieuw in via de Weekmenu-app; "
                            "daarna wordt het menu vanzelf afgemaakt.")
FINAL_FAILURE = ("Weekmenu kon niet gemaakt worden: Home Assistant/AH niet bereikbaar. "
                 "Tik op Nu maken in de Weekmenu-app.")
ALL_AWAY = ("Alle avonden van deze week staan op niet thuis (0 personen). "
            "Er is geen menu gemaakt.")
WRITE_FAILED_HINT = "Tik op Nu maken in de Weekmenu-app om de rest toe te voegen."
# v0.8: the daily "Vanavond" push to notify_today, and the Sunday order reminder
TONIGHT_AT = time(16, 0)
TONIGHT_LAST = time(20, 0)  # not after this (a start late in the day sends nothing)
REMINDER_AT = time(17, 0)
REMINDER_LAST = time(23, 0)
REMINDER = "Het menu staat klaar, maar er is nog geen bezorgmoment gekozen."
LOGIN_CACHE = 300  # seconds: tokens.json is read again at most this often (or on change)
STAPLES_TIMEOUT = 60  # seconds for the whole purchase-history step before the message
PORT = 8099
INGRESS_PROXY = "172.30.32.2"
# schema of state.json; 0 is a file from before 0.6
STATE_SCHEMA = 1


def migrate_state_v0_to_v1(data: dict) -> dict:
    """0 -> 1: only the version field (load_state already reads every older form)."""
    return {**data, "schema": 1}


_STATE_STEPS = {0: migrate_state_v0_to_v1}


def local_now() -> datetime:
    """Naive local time in the HA time zone (Supervisor sets TZ)."""
    tz = os.environ.get("TZ") or "Europe/Amsterdam"
    try:
        zone = ZoneInfo(tz)
    except Exception:
        zone = ZoneInfo("Europe/Amsterdam")
    return datetime.now(zone).replace(tzinfo=None)


@dataclass
class Waiting:
    """No bonus period yet for the menu week `week`; `expected`: AH's own date for it."""
    week: date
    expected: date | None


@dataclass
class Context:
    client: Any  # AhClient
    ledger: Ledger
    hass: Any  # Hass
    store: Any  # TokenStore
    pantry: list[str]
    total_path: Path = TOTAL_PATH
    state_path: Path | None = None
    schedule_time: time = DEFAULT_SCHEDULE
    dinner_time: time = DEFAULT_DINNER  # v0.8: "Vanavond" counts back from this
    ingress_url: str = "/hassio/ingress/weekmenu"
    clock: Callable[[], datetime] = local_now
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # status for api/status
    last_round: str | None = None
    error: str | None = None
    login_expired: bool = False
    busy: bool = False  # a round started from the web API, not finished yet
    in_round: bool = False  # a menu round holds the lock (not the hourly new-folder look)
    rounds: int = 0  # menu rounds that ended done, in error or with an expired login
    # schedule: the last hourly slot looked at, and the folder that is not out yet
    checked: datetime | None = None
    waiting: Waiting | None = None
    error_kind: str | None = None  # "read": a read that failed; a waiting result clears it
    away_notified: date | None = None  # menu week of the last "all evenings away" push
    total_day: date | None = None
    # bonus offers per period start: the folder is the same all week. Several periods
    # (the tab may show this week's folder while the round reads next week's), kept in
    # folder_path over a restart when that is set (foldercache.py)
    bonus_cache: dict[date, list[Product]] = field(default_factory=dict)
    folder_path: Path | None = None
    # menu week -> (start of the current period used instead of its own folder, when):
    # lets the Aanbiedingen tab use a filled cache without asking AH for the periods
    offers_fallback: dict[date, tuple[date, datetime]] = field(default_factory=dict)
    tasks: set = field(default_factory=set)
    # v0.6: names of files from a newer add-on (never written), and whether the one
    # "werk de add-on bij" message went out
    too_new: set[str] = field(default_factory=set)
    too_new_notified: bool = False
    planner_seconds: float | None = None  # the menu search of the last build_plan
    # v0.7: AH API health (shared with the client), and whether its one push went out
    health: ApiHealth = field(default_factory=ApiHealth)
    health_notified: bool = False
    app_version: str = APP_VERSION
    # v0.8, kept in state.json: the day "Vanavond" and the order reminder went out
    # (marked before the push: at most once a day, also over a restart)
    tonight_sent: date | None = None
    reminder_sent: date | None = None
    reminder_tried: datetime | None = None  # not kept: a failed read tries again hourly
    tonight_warned: datetime | None = None  # not kept: HA unreadable is logged hourly
    reminder_done: date | None = None  # not kept: the day's check found nothing to say
    published: tuple | None = None  # (day, login_ok) of the last sensor.weekmenu
    # (mtime of tokens.json, monotonic time read, a usable login in it) for login_view
    _login_cache: tuple | None = None
    _saved_marks: tuple | None = None  # what state.json holds of the marks above
    # one sync_health at a time: two requests at once must not both send the push
    _sync_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # v0.9: "Klopt niet" choices and the swap history, next to the week logs in /data
    overrides: Overrides | None = None
    history: History | None = None
    # products from the last "Ander product" searches: a choice must be one of these
    found: dict[int, Product] = field(default_factory=dict)
    # v1.0: duim omlaag and "nooit meer" per recipe, and "Besparing nagerekend"
    taste: Taste | None = None
    reconciliations: Reconciliations | None = None
    recon_checked: datetime | None = None  # not kept: the hour "nagerekend" last looked

    def __post_init__(self):
        data = self.ledger.root.parent
        if self.overrides is None:
            self.overrides = Overrides(data / "overrides.json")
        if self.history is None:
            self.history = History(data / "history.json")
        if self.taste is None:
            self.taste = Taste(data / "taste.json")
        if self.reconciliations is None:
            self.reconciliations = Reconciliations(data / "reconciliation.json")
        own = getattr(self.client, "health", None)
        if isinstance(own, ApiHealth):
            self.health = own  # one object: the client counts, the writer and web read
        else:
            try:
                self.client.health = self.health
            except AttributeError:
                pass
        self._saved_marks = _marks(self)


@dataclass
class RoundOutcome:
    status: Literal["done", "skipped", "waiting_for_folder", "login_expired", "error"]
    week: date | None = None
    expected: date | None = None  # waiting_for_folder: when the folder should be out
    kind: str = "round"  # of an error: "read" (nothing written) or "round"
    warning: str | None = None
    plan: Plan | None = None
    result: WriteResult | None = None
    error: str | None = None


# --- small helpers --------------------------------------------------------------------


async def guard(awaitable, what: str) -> bool:
    """Await a Home Assistant call; a failure is logged, never raised."""
    try:
        await awaitable
        return True
    except httpx.HTTPError as e:
        log.warning("Home Assistant: %s mislukt (%s)", what, type(e).__name__)
        return False


_ID_SEGMENT = re.compile(r"/\d+(?=[/?:\s]|$)")


def short_error(e: BaseException) -> str:
    """A description without secrets: AhClient errors never carry bodies or tokens, and
    a numeric path segment (an order id) becomes <id>, so none goes into the log or the
    diagnose."""
    if isinstance(e, AhApiError):
        return _ID_SEGMENT.sub("/<id>", str(e))[:200]
    return type(e).__name__


def note_too_new(ctx: Context, path: Path) -> None:
    """A file from a newer add-on was found (and is not written): log it once."""
    name = Path(path).name
    if name not in ctx.too_new:
        ctx.too_new.add(name)
        log.warning("%s is van een nieuwere versie van de add-on: wordt niet geschreven", name)


async def notify_too_new(ctx: Context) -> None:
    """The one message that the add-on is older than its data. The mark is kept in
    state.json, so it goes once, not once per process (unless state.json itself is the
    newer file: then this process can only remember it in memory)."""
    if ctx.too_new and not ctx.too_new_notified:
        if await guard(ctx.hass.notify("Weekmenu", UPDATE_MESSAGE, ctx.ingress_url),
                       "melding"):
            ctx.too_new_notified = True
            _save_state(ctx)


def scan_too_new(ctx: Context) -> None:
    """At startup: every file a newer add-on may have left, the week logs included.
    Nothing newer any more (the add-on was updated): the "werk bij" mark is cleared,
    so a later downgrade tells it again."""
    away = getattr(ctx.hass, "_away_path", None)
    files = [(ctx.total_path, TOTAL_SCHEMA), (ctx.state_path, STATE_SCHEMA),
             (away, AWAY_SCHEMA), (ctx.overrides.path, OVERRIDES_SCHEMA),
             (ctx.history.path, HISTORY_SCHEMA), (ctx.taste.path, TASTE_SCHEMA),
             (ctx.reconciliations.path, RECON_SCHEMA)]
    for path, known in files:
        if path is not None and too_new(path, known):
            note_too_new(ctx, path)
    if ctx.store.too_new():
        note_too_new(ctx, ctx.store.path)
    try:
        weeks = ctx.ledger.weeks()
    except Exception as e:
        log.warning("Logboeken niet te lezen (%s)", type(e).__name__)
        weeks = []
    for w in weeks:
        if w.too_new:
            note_too_new(ctx, ctx.ledger.root / f"{w.week.isoformat()}.json")
    if not ctx.too_new and ctx.too_new_notified:
        ctx.too_new_notified = False
        _save_state(ctx)


def update_needed(ctx: Context) -> bool:
    """A file (or the login) is from a newer add-on: the app shows "werk bij"."""
    return bool(ctx.too_new) or bool(ctx.store.too_new())


def _marks(ctx: Context) -> tuple:
    h = ctx.health
    return (h.writes_paused, h.paused_by, h.paused_at, ctx.health_notified,
            ctx.too_new_notified)


async def sync_health(ctx: Context) -> None:
    """The one "AH lijkt iets veranderd" push per pause, and the pause in state.json.

    Never raises and never calls AH. The push is marked sent only when it went out, so
    an HA failure tries again next time; when writing is on again the mark is cleared,
    so a later pause tells it again.
    """
    async with ctx._sync_lock:
        h = ctx.health
        if h.writes_paused and not ctx.health_notified:
            if await guard(ctx.hass.notify("Weekmenu", PAUSED_MESSAGE, ctx.ingress_url),
                           "melding"):
                ctx.health_notified = True
        elif not h.writes_paused and ctx.health_notified:
            ctx.health_notified = False
        if _marks(ctx) != ctx._saved_marks:
            _save_state(ctx)


def health_needs_sync(ctx: Context) -> bool:
    return (ctx.health.writes_paused != ctx.health_notified
            or _marks(ctx) != ctx._saved_marks)


async def probe_health(ctx: Context) -> None:
    """Writes are paused: one read round (the folder, the order or else the list, and the
    endpoint that paused it). When it all succeeds ApiHealth switches writing on again.
    Only reads; errors are logged, never raised."""
    h = ctx.health
    if not h.writes_paused:
        return
    async with ctx.lock:
        try:
            await ctx.client.bonus_periods()
            order = await ctx.client.active_order()
            if order is None or h.paused_by == "lijst":
                try:
                    await ctx.client.shopping_list()
                except AhOrderMode:
                    # the list is locked by the order that just read fine: not failing,
                    # and the order read stands in for it
                    h.ok("lijst")
            if h.writes_paused and h.paused_by == "recept":
                await ctx.client.search_recipes("pasta", 1)
            if h.writes_paused and h.paused_by == "product":
                await ctx.client.search_products("melk", 1)
        except Exception as e:
            log.warning("Leesronde terwijl schrijven stilstaat: %s", short_error(e))
    await sync_health(ctx)


def local_from_epoch(stamp: float) -> datetime:
    tz = os.environ.get("TZ") or "Europe/Amsterdam"
    try:
        zone = ZoneInfo(tz)
    except Exception:
        zone = ZoneInfo("Europe/Amsterdam")
    return datetime.fromtimestamp(stamp, zone).replace(tzinfo=None)


def _have_login(ctx: Context, stamp: float | None) -> bool:
    """Whether tokens.json holds a usable login; read again only when the file changed
    or after LOGIN_CACHE seconds (the schedule asks every minute)."""
    cached = ctx._login_cache
    clock = _time.monotonic()
    if cached is not None and cached[0] == stamp and clock - cached[1] < LOGIN_CACHE:
        return cached[2]
    have = stamp is not None and ctx.store.load() is not None
    ctx._login_cache = (stamp, clock, have)
    return have


def login_view(ctx: Context, now: datetime | None = None) -> dict:
    """"AH-login in orde, laatst ververst <datum>": `login_ok` is false only when there
    is no login or it expired (AH refused it, also on a refresh). How long ago it was
    refreshed is shown, never judged (Ruling 50)."""
    stamp = ctx.store.saved_at()
    refreshed = local_from_epoch(stamp) if stamp is not None else None
    expired = ctx.login_expired or not _have_login(ctx, stamp)
    return {
        "login_ok": not expired,
        "login_refreshed": refreshed.isoformat(timespec="minutes") if refreshed else None,
        "login_expired": expired,
    }


def tonight(ctx: Context, day: date, away=()) -> str | None:
    """The "Vanavond" line for `day`, from the menu week holding it."""
    try:
        wlog = ctx.ledger.load(week_of(day))
    except Exception as e:
        log.warning("Logboek niet te lezen (%s)", type(e).__name__)
        return None
    if not wlog.plan:
        return None
    return vanavond(Plan.from_dict(wlog.plan), day, away, ctx.dinner_time)


def sensor_extras(ctx: Context, away=(), now: datetime | None = None) -> dict:
    """What sensor.weekmenu carries besides the menu: tonight and the login."""
    now = now or ctx.clock()
    login = login_view(ctx, now)
    return {"vanavond": tonight(ctx, now.date(), away),
            "login_ok": login["login_ok"], "login_refreshed": login["login_refreshed"]}


async def save_log(ctx: Context, wlog: WeekLog) -> None:
    """Ledger.save in a worker thread (it fsyncs). Only called with ctx.lock held, so no
    other writer can run meanwhile; the awaited save still ends before the next step.

    Never cancel it and never put it in asyncio.wait_for: a cancelled await leaves the
    thread writing on while the caller goes on (and may release the lock), so a second
    save could overlap it."""
    await asyncio.to_thread(ctx.ledger.save, wlog)


def week_of(day: date) -> date:
    """The Wednesday that starts the menu week holding `day`."""
    return day - timedelta(days=(day.weekday() - 2) % 7)


def make_settings(ctx: Context, raw: RawSettings, start: date, exclude=()) -> Settings:
    """Cook days from the eat days and the cooking moments (raw.aantal, bounded by
    plan_days). A cook day with a leftover night cooks for the persons of both days."""
    def persons_on(d: date) -> int:
        return max(1, raw.persons.get(WEEKDAGEN[d.weekday()], 2))

    pairs = plan_days(eat_days(start, raw.away), raw.aantal)
    days = [cook for cook, _ in pairs]
    leftovers = {cook: rest for cook, rest in pairs if rest is not None}
    persons = {cook: persons_on(cook) + (persons_on(rest) if rest else 0)
               for cook, rest in pairs}
    recent = ctx.ledger.recent_recipe_ids(start) | set(exclude)
    return Settings(days, persons, list(ctx.pantry), recent, leftovers,
                    overrides=ctx.overrides.load(), never=ctx.taste.never(),
                    down=ctx.taste.down())


def settings_from_plan(ctx: Context, plan: Plan, start: date) -> Settings:
    """The cook days, persons (also changed per dish) and leftover nights of an existing
    menu, for a new menu over the same evenings ("Ander gerecht", "Menu bijwerken")."""
    return Settings(
        evenings=sorted(x.date for x in plan.dishes),
        persons={x.date: x.persons for x in plan.dishes},
        leftovers={x.date: x.leftover_date for x in plan.dishes if x.leftover_date},
        pantry=list(plan.pantry or ctx.pantry),
        recent_recipe_ids=ctx.ledger.recent_recipe_ids(start),
        overrides=ctx.overrides.load(),
        never=ctx.taste.never(),
        down=ctx.taste.down(),
    )


def _period_for(periods: list[BonusPeriod], day: date) -> BonusPeriod | None:
    return next((p for p in periods if p.start <= day <= p.end), None)


def last_check(ctx: Context) -> time:
    return max(LAST_CHECK, ctx.schedule_time)


def check_slot(now: datetime, at: time, last: time = LAST_CHECK) -> datetime | None:
    """The hourly look `now` belongs to: `at`, then every full hour, the last at 18:00.

    None before `at`. After the last look of the day, the last look itself: a start
    at 20:00 still gets the 18:00 look (on the Sunday: the fallback).
    """
    last = max(last, at)
    day = now.date()
    if now.time() < at:
        return None
    if now.time() >= last:
        return datetime.combine(day, last)
    return max(now.replace(minute=0, second=0, microsecond=0), datetime.combine(day, at))


def is_fallback_day(day: date, start: date) -> bool:
    return day == start - timedelta(days=FALLBACK_DAYS)


def last_try(ctx: Context, now: datetime, start: date) -> bool:
    """The last automatic try for this menu week: the fallback Sunday, 23:00."""
    return (is_fallback_day(now.date(), start)
            and now.time() >= max(LAST_FALLBACK_TRY, ctx.schedule_time))


def fallback_due(ctx: Context, now: datetime, start: date) -> bool:
    """The Sunday before the menu Wednesday, from 18:00: the current folder may be used."""
    return is_fallback_day(now.date(), start) and now.time() >= last_check(ctx)


def auto_done(wlog: WeekLog) -> bool:
    """Nothing left for the automatic round: a finished round, or its one completion."""
    return bool(wlog.plan) and (wlog.round_done or wlog.completion_tried)


async def bonus_for_week(
    ctx: Context, start: date, now: datetime, fallback: bool
) -> tuple[list[Product], str | None] | Waiting:
    """The offers of the period holding the menu Wednesday, and the warning (or None)."""
    got = await folder_for_week(ctx, start, now, fallback)
    return got if isinstance(got, Waiting) else got[:2]


async def folder_for_week(
    ctx: Context, start: date, now: datetime, fallback: bool
) -> tuple[list[Product], str | None, date] | Waiting:
    """The offers of the period holding the menu Wednesday, the warning, and the start
    of the period used.

    Not out yet: `Waiting` with AH's expected date (nextPeriodVisibleFrom of the
    current period), and no bonus_offers call. With `fallback` the current period
    instead, with the warning for the message.
    """
    periods = await ctx.client.bonus_periods()
    period = _period_for(periods, start)
    warning = None
    if period is None:
        current = _period_for(periods, now.date())
        if not fallback:
            return Waiting(start, current.next_visible_from if current else None)
        if current is None:
            raise AhApiError("geen bonusfolder gevonden voor deze week")
        period = current
        warning = PERIOD_WARNING
    elif ctx.waiting is not None and ctx.waiting.week == start:
        ctx.waiting = None  # the folder is out
    if period.start not in ctx.bonus_cache:
        items = await ctx.client.bonus_offers(period)
        ctx.bonus_cache = remember(ctx.bonus_cache, period.start, items)
        if ctx.folder_path is not None:
            await asyncio.to_thread(save_folders, ctx.folder_path, ctx.bonus_cache)
    return ctx.bonus_cache[period.start], warning, period.start


def app_week(ctx: Context, now: datetime) -> date:
    """The week the app shows by default: the running one when it has a menu, else the
    next menu week (not shown_week below: that is the sensor's choice)."""
    current = week_of(now.date())
    return current if ctx.ledger.load(current).plan else menu_week_start(now)


async def warm_folder(ctx: Context) -> None:
    """At start, in the background: read the folder the Aanbiedingen tab will show when
    it is not kept yet, so opening the tab does not wait minutes. Only with a usable
    login; a failure is left to the tab itself (which then reads it as it always did)."""
    now = ctx.clock()
    if not login_view(ctx, now)["login_ok"]:
        return
    try:
        start = app_week(ctx, now)
        async with ctx.lock:
            if any(first <= start <= first + timedelta(days=6) for first in ctx.bonus_cache):
                return
            await folder_for_week(ctx, start, now, fallback=True)
        log.info("Folder voor de week van %s voorgeladen", start)
    except Exception as e:
        log.info("Folder voorladen lukte niet (%s); de tab haalt hem zelf op",
                 type(e).__name__)


# --- the round --------------------------------------------------------------------------


async def run_round(ctx: Context, now: datetime, mode: Mode) -> RoundOutcome:
    """One round for the next menu week.

    "schema" is the automatic hourly look: it does nothing once the log has a menu
    for that week, and before the Sunday 18:00 fallback a failure that wrote nothing
    is left for the next hour, without a message. The buttons nu_maken and nieuw_menu
    always make a menu, with the current folder while next week's is not out.
    "bijwerken" is nieuw_menu with the week's own folder, and only when the menu is
    from an older folder and that folder is out (new_folder_available); else it skips.
    """
    start = menu_week_start(now)
    fallback = (mode in ("nu_maken", "nieuw_menu")
                or (mode == "schema" and fallback_due(ctx, now, start)))
    try:
        raw = await ctx.hass.read_settings()
    except Exception as e:  # not only HTTP: nothing may stop the schedule silently
        msg = f"Home Assistant-instellingen niet te lezen ({type(e).__name__})"
        ctx.error, ctx.error_kind = msg, "read"
        ctx.rounds += 1  # a round that ended in an error, for the app
        if mode != "schema":
            log.warning(msg)
        elif last_try(ctx, now, start):
            log.error("%s; geen menu voor de week van %s", msg, start)
            await guard(ctx.hass.notify("Weekmenu mislukt", FINAL_FAILURE, ctx.ingress_url),
                        "melding")
        else:
            log.warning("%s, volgend uur opnieuw", msg)
        return RoundOutcome("error", start, error=msg)
    if mode == "schema" and not raw.enabled:
        log.info("Weekmenu staat uit: geen automatische ronde")
        return RoundOutcome("skipped", start)

    async with ctx.lock:
        ctx.in_round = True
        try:
            outcome = await _round(ctx, now, mode, start, raw, fallback)
        finally:
            ctx.in_round = False
    if outcome.status in ("done", "error", "login_expired"):
        ctx.last_round = now.isoformat(timespec="minutes")
        ctx.rounds += 1
    if outcome.status == "done":
        ctx.error = ctx.error_kind = None
        ctx.login_expired = False
        if ctx.waiting is not None and ctx.waiting.week == start:
            ctx.waiting = None
    elif outcome.status == "waiting_for_folder":
        if ctx.error_kind == "read":  # HA and AH answered now; other errors stay
            ctx.error = ctx.error_kind = None
        ctx.waiting = Waiting(start, outcome.expected)
    elif outcome.status == "login_expired":
        ctx.error, ctx.error_kind = outcome.error, "login"
    elif outcome.status == "error":
        ctx.error, ctx.error_kind = outcome.error, outcome.kind
    if outcome.status != "skipped":
        log.info("Ronde %s voor week %s: %s", mode, start, outcome.status)
    if outcome.status in ("done", "waiting_for_folder"):
        _save_state(ctx)
    await update_total(ctx, now.date())
    await sync_health(ctx)
    return outcome


async def _round(ctx: Context, now: datetime, mode: Mode, start: date, raw: RawSettings,
                 fallback: bool):
    warning = None
    had_error = ctx.error is not None
    reused = False
    completing = False
    wlog = None
    try:
        wlog = ctx.ledger.load(start)
        if wlog.too_new or ctx.store.too_new():
            # v0.6: a newer add-on wrote this week or the login: write nothing at all
            note_too_new(ctx, ctx.ledger.root / f"{start.isoformat()}.json"
                         if wlog.too_new else ctx.store.path)
            await notify_too_new(ctx)
            return RoundOutcome("error", start, error=UPDATE_MESSAGE, kind="schema")
        if mode == "schema" and auto_done(wlog):
            return RoundOutcome("skipped", start)  # the log says done: never again
        if mode == "bijwerken" and not new_folder_available(wlog):
            log.info("Geen nieuwe folder voor de week van %s: menu niet bijgewerkt", start)
            return RoundOutcome("skipped", start)
        if mode == "schema" and wlog.plan:
            # a round stopped halfway (crash or write error): complete it once, from the
            # log (pending is settled first, so no write is ever repeated blind)
            completing = True
            wlog.completion_tried = True
            await save_log(ctx, wlog)
            log.info("Ronde voor week %s was niet afgemaakt: één keer aanvullen", start)
        if mode not in REBUILD and wlog.plan:
            reused = True
            plan, replaced = Plan.from_dict(wlog.plan), None  # same menu, only fill gaps
        else:
            replaced = Plan.from_dict(wlog.plan) if wlog.plan else None
            if mode == "bijwerken":
                # the same evenings, persons, leftover nights and removed days: only the
                # folder changes (spec: "houdt de eetdagen en personen aan")
                settings = settings_from_plan(ctx, replaced, start)
                if not settings.evenings:  # every dish was removed: nothing to update
                    log.info("Geen gerechten in de week van %s: niets bij te werken", start)
                    return RoundOutcome("skipped", start)
            elif not make_settings(ctx, raw, start).evenings:  # nobody eats at home
                log.info("Alle avonden van de week van %s zijn niet thuis: geen menu", start)
                if mode != "schema" or ctx.away_notified != start:
                    ctx.away_notified = start
                    await guard(ctx.hass.notify("Weekmenu", ALL_AWAY, ctx.ingress_url), "melding")
                return RoundOutcome("skipped", start)
            got = await folder_for_week(ctx, start, now, fallback=fallback)
            if isinstance(got, Waiting):
                log.info("Folder voor de week van %s nog niet uit (verwacht %s)",
                         start, got.expected or "onbekend")
                return RoundOutcome("waiting_for_folder", start, expected=got.expected)
            bonus, warning, period_start = got
            if mode == "bijwerken":
                plan = await build_plan(ctx.client, bonus, settings)
                # removed evenings stay "geen gerecht": no dish, nothing bought
                plan.removed_days = sorted(replaced.removed_days)
            else:
                # Nieuw menu asks for other dishes
                old_ids = {d.recipe.id for d in replaced.dishes} if replaced else set()
                plan = await build_plan(ctx.client, bonus,
                                        make_settings(ctx, raw, start, old_ids))
            if plan.search_seconds is not None:
                ctx.planner_seconds = round(plan.search_seconds, 2)
            if not plan.dishes:
                raise AhApiError("geen enkel recept gevonden dat in de regels past")
            wlog.period_warning = warning  # a menu from the right folder clears it
            wlog.folder_period_start = period_start  # and so "new folder available"
            if replaced is not None:  # a new menu: the swap history was about the old one
                wlog.swapped_out.clear()
                wlog.previous.clear()
                wlog.previous_dish.clear()
            # a new menu is a new round: apply saves these with the plan, before writing,
            # so a crash halfway is completed by the next automatic slot
            wlog.round_done = wlog.completion_tried = wlog.notified = False
        result = await apply(ctx.client, ctx.ledger, wlog, plan, replaced, today=now.date())
    except AhLoginExpired:
        known = ctx.login_expired
        ctx.login_expired = True
        log.warning("AH-login verlopen")
        if completing:  # not a real try: after logging in again the next slot completes
            wlog.completion_tried = False
            try:
                await save_log(ctx, wlog)
            except Exception as e:
                log.warning("Logboek %s niet bijgewerkt (%s)", start, type(e).__name__)
        if mode != "schema" or not known:  # the hourly look reports it once
            text = LOGIN_EXPIRED_COMPLETION if completing else LOGIN_EXPIRED
            await guard(ctx.hass.notify("Weekmenu", text, ctx.ingress_url), "melding")
        return RoundOutcome("login_expired", start, error=LOGIN_EXPIRED)
    except Exception as e:
        msg = short_error(e)
        written = wlog is not None and wlog.plan is not None  # apply saves it first
        if mode == "schema" and not written:
            if not last_try(ctx, now, start):
                log.warning("Ronde mislukt: %s; volgend uur opnieuw", msg)
                return RoundOutcome("error", start, error=msg, kind="read")
            log.error("Ronde mislukt: %s; geen menu voor de week van %s", msg, start)
            await guard(ctx.hass.notify("Weekmenu mislukt", FINAL_FAILURE, ctx.ingress_url),
                        "melding")
            return RoundOutcome("error", start, error=msg, kind="read")
        log.error("Ronde mislukt: %s", msg)
        if ctx.health.writes_paused:  # AH changed: the one ApiHealth push says it
            return RoundOutcome("error", start, error=msg)
        text = (f"Er ging iets mis: {msg}. Er is verder niets toegevoegd; "
                "wat al gelukt was staat in het logboek.")
        if written:
            text += " " + WRITE_FAILED_HINT
        await guard(ctx.hass.notify("Weekmenu mislukt", text, ctx.ingress_url), "melding")
        return RoundOutcome("error", start, error=msg)

    held = result.held is not None
    if held:
        # Ruling 42: the open order is for another week (or its day is unknown), so
        # nothing was written. round_done stays False: every hourly look tries again,
        # from the log, until there is an order for this week or the list is usable.
        log.info("Week %s: menu bewaard, nog niets toegevoegd (%s)", start, wlog.held)
        if completing:
            wlog.completion_tried = False  # not a real try
            try:
                await save_log(ctx, wlog)
            except Exception as e:
                log.warning("Logboek %s niet bijgewerkt (%s)", start, type(e).__name__)
    # before the message, which names the count; bounded so the message is never late.
    # A held completion skips it: the hourly retry would read the history every hour.
    if not (held and completing):
        await refresh_staples(ctx, wlog, plan, start, now)
    await recount_extras(ctx, wlog)  # apply may have settled a staple write
    view = plan_view(plan, start, result.target, away_dates(start, raw.away),
                     new_folder=new_folder_available(wlog))
    view.update(sensor_extras(ctx, raw.away, now))
    await guard(ctx.hass.publish(view, plan.savings + wlog.extras_total), "sensoren")
    nothing_new = not (result.added or result.removed or result.pending or result.unknown)
    if completing:  # the message only if it never went out, or something changed
        send = not (wlog.notified and nothing_new)
    else:
        send = not (reused and nothing_new and not had_error)
    if not send:
        log.info("Menu stond er al en alles was al toegevoegd: geen nieuwe melding")
    else:
        # no count when the history failed or took too long (staples_error is set then)
        title, body = format_message(plan, result, warning,
                                     staples=staples.open_count(wlog, plan.basket),
                                     start=start, away=away_dates(start, raw.away))
        if await guard(ctx.hass.notify(title, body, ctx.ingress_url), "melding"):
            wlog.notified = True  # saved at once: a crash now must not repeat the message
            try:
                await save_log(ctx, wlog)
            except Exception as e:
                log.warning("Logboek %s niet bijgewerkt (%s)", start, type(e).__name__)
    if not wlog.round_done and not held:
        wlog.round_done = True
        try:
            await save_log(ctx, wlog)
        except Exception as e:
            log.warning("Logboek %s niet bijgewerkt (%s)", wlog.week, type(e).__name__)
    return RoundOutcome("done", start, warning=warning, plan=plan, result=result)


async def recount_extras(ctx: Context, wlog: WeekLog) -> None:
    """extras_savings from the staples in extras_added (a settle can add one)."""
    new = staples.extras_savings(wlog)
    if new != wlog.extras_savings:
        wlog.extras_savings = new
        try:
            await save_log(ctx, wlog)
        except Exception as e:
            log.warning("Logboek %s niet bijgewerkt (%s)", wlog.week, type(e).__name__)


async def refresh_staples(ctx: Context, wlog: WeekLog, plan: Plan, start: date,
                          now: datetime) -> None:
    """Vaste boodschappen: read the purchase history after the menu and suggest.

    Never breaks the round: any failure (also of AH itself), or taking longer than
    STAPLES_TIMEOUT seconds in all, leaves no suggestions, `staples_error` on the week
    and a log line. Staples already added stay listed. Only reads, so cutting it off
    halfway is safe.
    """
    today = now.date()

    async def fetch() -> list:
        periods = await ctx.client.bonus_periods()
        period = _period_for(periods, start) or _period_for(periods, today)
        if period is None:
            raise AhApiError("geen bonusfolder gevonden voor deze week")
        bonus = await ctx.client.previously_bought_bonus(period)
        bought = await ctx.client.purchases(today - timedelta(days=staples.WINDOW_DAYS))
        return staples.suggest_staples(bought, bonus, plan.basket, today)

    try:
        # Outside the time limit: a cancelled refresh could lose the rotated token.
        await ctx.client.ensure_fresh()
        staples.merge(wlog, await asyncio.wait_for(fetch(), STAPLES_TIMEOUT))
        wlog.staples_error = None
    except Exception as e:
        msg = ("duurde langer dan {} s".format(STAPLES_TIMEOUT)
               if isinstance(e, asyncio.TimeoutError) else short_error(e))
        log.warning("Vaste boodschappen niet opgehaald: %s", msg)
        staples.merge(wlog, [])
        wlog.staples_error = f"Vaste boodschappen niet op te halen ({msg})"
    try:
        await save_log(ctx, wlog)
    except Exception as e:
        log.warning("Logboek %s niet bijgewerkt (%s)", wlog.week, type(e).__name__)


async def update_total(ctx: Context, today: date) -> None:
    """Add what every started week saved and was not counted yet, then publish the total.

    The source of truth is `counted` in totaal.json: {ISO week: amount counted},
    written in the same atomic replace as the total, so a crash can never count an
    amount twice. A week adds max(0, its value now - what was counted), so a staple
    added after the week started still reaches the total, once; nothing is ever
    subtracted. Older files (a list of weeks, or no list) are migrated with the
    ledger values at migration time. The ledger `counted` flag is only for display.
    The file is written before the sensor POST, so an HA error still leaves the weeks
    counted; a failing file write counts nothing.
    """
    async with ctx.lock:
        total, known = load_total_state(ctx.total_path)
        # a week from a newer add-on is left to that add-on: not counted, not marked
        weeks = [w for w in ctx.ledger.weeks() if not w.too_new]
        value = {w.week.isoformat(): round(w.savings + w.extras_total, 2) for w in weeks}
        if known is None:
            known = {w.week.isoformat() for w in weeks if w.counted}
        if isinstance(known, set):
            counted = {iso: value.get(iso, 0.0) for iso in known}
        else:
            counted = dict(known)
        new = [w for w in weeks if w.week <= today and w.week.isoformat() not in counted]
        added = 0.0
        for w in weeks:
            iso = w.week.isoformat()
            if w.week > today:
                continue
            more = round(value[iso] - counted.get(iso, 0.0), 2)
            if iso not in counted or more > 0:
                added += max(0.0, more)
                counted[iso] = max(value[iso], counted.get(iso, 0.0))
        total = round(total + added, 2)
        try:
            await ctx.hass.publish_total(total, counted)
        except httpx.HTTPError as e:
            log.warning("Home Assistant: totaal niet gezet (%s)", type(e).__name__)
        except SchemaTooNew as e:
            note_too_new(ctx, e.path)
            return
        except Exception as e:
            log.error("Totaal niet opgeslagen: %s", type(e).__name__)
            return
        ctx.total_day = today
        for w in new:  # display only; the file above decides
            if not w.counted:
                w.counted = True
                try:
                    await save_log(ctx, w)
                except Exception as e:
                    log.warning("Logboek %s niet bijgewerkt (%s)", w.week, type(e).__name__)


async def check_new_folder(ctx: Context, start: date) -> None:
    """The menu of `start` exists and was made with an older folder (period_warning):
    look whether the week's own folder is out. If so, note it on the week, publish the
    flag and send one push per week. Never changes the menu, and never raises.

    Only one AH read an hour, and none once the new folder is known or the menu was
    made with the right folder. The push is marked sent only when it went out, like the
    "klaar" message, so an HA failure tries again the next hour.
    """
    async with ctx.lock:
        wlog = ctx.ledger.load(start)
        if not wlog.plan or wlog.period_warning is None or wlog.too_new:
            return
        if wlog.new_folder is None:
            try:
                period = _period_for(await ctx.client.bonus_periods(), start)
            except Exception as e:
                log.warning("Nieuwe folder niet bekeken: %s", short_error(e))
                return
            if period is None or (wlog.folder_period_start is not None
                                  and period.start <= wlog.folder_period_start):
                return
            wlog.new_folder = period.start
            try:
                await save_log(ctx, wlog)
            except Exception as e:  # not noted: no push either, the next hour tries again
                log.warning("Logboek %s niet bijgewerkt (%s)", start, type(e).__name__)
                return
            log.info("De folder van de week van %s is er; het menu is met een eerdere folder gemaakt", start)
            await publish_week(ctx, wlog)
        if not new_folder_available(wlog) or wlog.new_folder_notified:
            return
        if await guard(ctx.hass.notify("Weekmenu", NEW_FOLDER, ctx.ingress_url), "melding"):
            wlog.new_folder_notified = True
            try:
                await save_log(ctx, wlog)
            except Exception as e:
                log.warning("Logboek %s niet bijgewerkt (%s)", start, type(e).__name__)


async def notify_stale_held(ctx: Context, start: date) -> None:
    """Ruling 44: a held menu of the week before the target week is never retried.

    Once per week (held_notified, saved): one push with what to add by hand.
    """
    prev = start - timedelta(days=7)
    # load, push and save under the lock: a web write to that week in between would
    # otherwise be overwritten by this older copy of the log
    async with ctx.lock:
        wlog = ctx.ledger.load(prev)
        if not (wlog.plan and wlog.held) or wlog.held_notified or wlog.too_new:
            return
        plan = Plan.from_dict(wlog.plan)
        todo = writer.missing(wlog, plan)
        if not todo:
            return
        titles = [plan.products[p].title if p in plan.products else str(p) for p in todo]
        if await guard(ctx.hass.notify("Weekmenu", writer.manual_note(titles),
                                       ctx.ingress_url), "melding"):
            wlog.held_notified = True
            wlog.held_notified_items = sorted(todo)
            try:
                await save_log(ctx, wlog)
            except Exception as e:
                log.warning("Logboek %s niet bijgewerkt (%s)", prev, type(e).__name__)


async def close_stale_new_folder(ctx: Context, start: date) -> None:
    """The target week moved on (Monday): the week before can no longer be "bijgewerkt".
    Its flag is cleared once, and while the sensor still shows that week (no menu for
    the new target yet) it is published again without the flag."""
    async with ctx.lock:
        old = ctx.ledger.load(start - timedelta(days=7))
        if not new_folder_available(old) or old.too_new:
            return
        old.new_folder = None
        try:
            await save_log(ctx, old)
        except Exception as e:
            log.warning("Logboek %s niet bijgewerkt (%s)", old.week, type(e).__name__)
            return
        log.info("Week van %s is voorbij voor Menu bijwerken: melding vervalt", old.week)
        if not ctx.ledger.load(start).plan:
            await publish_week(ctx, old)


async def publish_week(ctx: Context, wlog: WeekLog) -> bool:
    """sensor.weekmenu for this week's menu as it is, with the new-folder flag."""
    try:
        raw = await ctx.hass.read_settings()
    except Exception as e:
        log.warning("Home Assistant-instellingen niet te lezen (%s)", type(e).__name__)
        return False
    plan = Plan.from_dict(wlog.plan)
    view = plan_view(plan, wlog.week, wlog.target or "list", away_dates(wlog.week, raw.away),
                     new_folder=new_folder_available(wlog))
    view.update(sensor_extras(ctx, raw.away))
    return await guard(ctx.hass.publish(view, plan.savings + wlog.extras_total), "sensoren")


def shown_week(ctx: Context, now: datetime) -> date:
    """The week sensor.weekmenu shows: the next menu week once it has a menu."""
    nxt = menu_week_start(now)
    return nxt if ctx.ledger.load(nxt).plan else week_of(now.date())


async def republish(ctx: Context, now: datetime) -> None:
    """A new day or a change in the login: sensor.weekmenu again, so "vanavond" and
    the login line on the dashboard card follow. Only when there is a menu to show."""
    key = (now.date(), login_view(ctx, now)["login_ok"])
    if key == ctx.published:
        return
    # load and post under the lock: a round (or a web write) that publishes a newer
    # menu meanwhile is never overwritten by this older copy
    async with ctx.lock:
        wlog = ctx.ledger.load(shown_week(ctx, now))
        if not wlog.plan or wlog.too_new:
            ctx.published = key
            return
        if await publish_week(ctx, wlog):
            ctx.published = key


async def notify_tonight(ctx: Context, now: datetime) -> None:
    """Every day from 16:00 (until 20:00): "Vanavond: ..." to the notify_today targets.

    At most once a day: the day is saved in state.json before the push goes out (no
    push at all when it cannot be saved). Not at home, no dish or no cooking moment:
    nothing, and nothing more that day. Home Assistant unreadable: not marked, so the
    next minute tries again; the warning is logged at most once an hour, so it does not
    push every other line out of the diagnose.
    """
    today = now.date()
    targets = list(getattr(ctx.hass, "today", []) or [])
    if (ctx.tonight_sent == today or not targets
            or not TONIGHT_AT <= now.time() < max(TONIGHT_LAST, ctx.dinner_time)):
        return
    try:
        away = (await ctx.hass.read_settings()).away
    except Exception as e:  # not marked: the next minute tries again
        if ctx.tonight_warned is None or now - ctx.tonight_warned >= timedelta(hours=1):
            ctx.tonight_warned = now
            log.warning("Vanavond: Home Assistant-instellingen niet te lezen (%s)",
                        type(e).__name__)
        return
    text = tonight(ctx, today, away)
    ctx.tonight_sent = today
    if not _save_state(ctx):
        log.warning("Vanavond niet verstuurd: de status kon niet worden opgeslagen")
        return
    if text is None:
        return
    for target in targets:
        await guard(ctx.hass.notify_to(target, "Weekmenu", text, ctx.ingress_url), "melding")


async def remind_order(ctx: Context, now: datetime) -> None:
    """Sunday from 17:00 until 23:00: a menu for the coming menu week but no active
    order delivered in that week yet: one push to the owner. Only reads AH (the order
    and its day), at most once an hour, so a menu made at 18:00 (the fallback) still
    gets its reminder that evening.

    At most once a day (saved in state.json before the push). An order whose day is
    unknown counts as chosen (no guess).
    """
    today = now.date()
    if (now.weekday() != 6 or now.time() < REMINDER_AT
            or today in (ctx.reminder_sent, ctx.reminder_done)):
        return
    if now.time() >= REMINDER_LAST:
        ctx.reminder_done = today  # the day is over for the reminder
        return
    if ctx.reminder_tried is not None and now - ctx.reminder_tried < timedelta(hours=1):
        return
    start = menu_week_start(now)
    wlog = ctx.ledger.load(start)
    if not wlog.plan or wlog.too_new or ctx.login_expired or ctx.store.load() is None:
        return  # looked again next minute: only the log and the login file, no AH
    ctx.reminder_tried = now
    async with ctx.lock:
        try:
            order = await ctx.client.active_order()
            day = await ctx.client.order_delivery_date(order) if order is not None else None
        except Exception as e:
            log.warning("Bestelherinnering: bestelling niet te lezen (%s)", short_error(e))
            return
    if order is not None and (day is None or start <= day <= start + timedelta(days=6)):
        return  # chosen (or unknown): looked at again an hour later
    ctx.reminder_sent = ctx.reminder_done = today
    if not _save_state(ctx):
        log.warning("Bestelherinnering niet verstuurd: de status kon niet worden opgeslagen")
        return
    await guard(ctx.hass.notify("Weekmenu", REMINDER, ctx.ingress_url), "melding")


async def reconcile_due(ctx: Context, now: datetime) -> None:
    """v1.0: recount the savings of a delivered menu week (see app/reconcile.py)."""
    await reconcile.reconcile_due(ctx, now)


# --- schedule, state and stdin ----------------------------------------------------------


def load_state(ctx: Context) -> None:
    """state.json: {"checked": ISO datetime, "waiting": {"week", "expected"},
    "too_new_notified": bool, "api": {"writes_paused", "paused_by", "paused_at",
    "notified"}} (the last two since 0.7, optional: same schema version).

    An older file ({"schedule_done": ...}) or a broken one loads as empty: the log
    still knows which weeks have a menu, so nothing runs twice.
    """
    if ctx.state_path is None:
        return
    try:
        data = json.loads(Path(ctx.state_path).read_text(encoding="utf-8"))
        if schema_of(data) <= STATE_SCHEMA:  # a newer file is read as it is
            data = migrate(data, STATE_SCHEMA, _STATE_STEPS)
        checked = data.get("checked")
        waiting = data.get("waiting")
        ctx.checked = datetime.fromisoformat(checked) if checked else None
        ctx.too_new_notified = data.get("too_new_notified") is True
        api = data.get("api")
        if isinstance(api, dict):
            ctx.health.restore(api.get("writes_paused") is True, api.get("paused_by"),
                               api.get("paused_at"))
            ctx.health_notified = api.get("notified") is True
            ctx.health.restore_last_ok(api.get("last_ok"))
        ctx._saved_marks = _marks(ctx)
        ctx.tonight_sent = _day_or_none(data.get("tonight_sent"))
        ctx.reminder_sent = _day_or_none(data.get("reminder_sent"))
        if isinstance(waiting, dict):
            exp = waiting.get("expected")
            ctx.waiting = Waiting(date.fromisoformat(waiting["week"]),
                                  date.fromisoformat(exp) if exp else None)
    except (FileNotFoundError, KeyError, ValueError, TypeError, AttributeError):
        pass


def _day_or_none(value) -> date | None:
    try:
        return date.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        return None


def _save_state(ctx: Context) -> bool:
    """state.json, durably; False when it could not be written."""
    if ctx.state_path is None:
        return True
    w = ctx.waiting
    data = {
        "checked": ctx.checked.isoformat(timespec="minutes") if ctx.checked else None,
        "waiting": None if w is None else {
            "week": w.week.isoformat(),
            "expected": w.expected.isoformat() if w.expected else None},
        "too_new_notified": ctx.too_new_notified,
        "api": {**ctx.health.persisted(), "notified": ctx.health_notified,
                "last_ok": ctx.health.last_ok},
        # v0.8 (optional: same schema version)
        "tonight_sent": ctx.tonight_sent.isoformat() if ctx.tonight_sent else None,
        "reminder_sent": ctx.reminder_sent.isoformat() if ctx.reminder_sent else None,
    }
    try:
        write_json(Path(ctx.state_path), data, STATE_SCHEMA)
        ctx._saved_marks = _marks(ctx)
        return True
    except SchemaTooNew as e:
        note_too_new(ctx, e.path)
    except OSError as e:
        log.warning("Status niet opgeslagen (%s)", type(e).__name__)
    return False


async def tick(ctx: Context, now: datetime) -> None:
    """One look at the clock: the total once a day, and the hourly look for the menu.

    At most one look per slot, also over a restart (the slot is saved after the
    round). A menu in the log for the next menu week ends the looking for a menu; a
    menu from an older folder is then only looked at for the new folder.
    """
    if ctx.total_day != now.date():
        await update_total(ctx, now.date())
    if health_needs_sync(ctx):  # a web read may have paused writing: tell it now
        await sync_health(ctx)
    try:
        await _slot(ctx, now)
    finally:
        # v0.8, after the menu look: none of these may ever keep a menu from being made;
        # v1.0: "Besparing nagerekend" (read only, at most once per menu week)
        for extra in (republish, notify_tonight, remind_order, reconcile_due):
            try:
                await extra(ctx, now)
            except Exception as e:
                log.warning("%s mislukt: %s", extra.__name__, short_error(e))


async def _slot(ctx: Context, now: datetime) -> None:
    """The hourly look for the menu of the next menu week (at most one per slot)."""
    start = menu_week_start(now)
    last = LAST_FALLBACK_TRY if is_fallback_day(now.date(), start) else LAST_CHECK
    slot = check_slot(now, ctx.schedule_time, last)
    if slot is None or slot == ctx.checked:
        return
    ctx.checked = slot
    await probe_health(ctx)  # writes paused: one read round may switch them on again
    await close_stale_new_folder(ctx, start)
    await notify_stale_held(ctx, start)
    if not auto_done(ctx.ledger.load(start)):
        await run_round(ctx, now, "schema")
    else:
        await check_new_folder(ctx, start)
    _save_state(ctx)  # after the round: a crash halfway looks again after the restart
    await notify_too_new(ctx)


async def schedule_loop(ctx: Context) -> None:
    while True:
        now = ctx.clock()
        try:
            await tick(ctx, now)
        except Exception as e:
            log.error("Schema: %s", short_error(e))
        await asyncio.sleep(60 - ctx.clock().second)


async def handle_command(ctx: Context, line: str) -> RoundOutcome | None:
    """One stdin line: {"command": "nu_maken"}, "nieuw_menu" or "bijwerken"."""
    line = line.strip()
    if not line:
        return None
    try:
        data = json.loads(line)
    except ValueError:
        data = None
    cmd = data.get("command") if isinstance(data, dict) else data
    if cmd not in BUTTONS:
        log.warning("Onbekend stdin-commando genegeerd")
        return None
    return await run_round(ctx, ctx.clock(), cmd)


async def stdin_loop(ctx: Context) -> None:
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader()
    try:
        await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)
    except (ValueError, OSError) as e:
        log.warning("Geen stdin (%s): knoppen via hassio.addon_stdin werken niet", e)
        return
    while line := await reader.readline():
        try:
            await handle_command(ctx, line.decode("utf-8", "replace"))
        except Exception as e:
            log.error("Commando mislukt: %s", short_error(e))


# --- startup ------------------------------------------------------------------------------


def load_options(path: Path = OPTIONS_PATH) -> tuple[list[str], time]:
    try:
        opts = json.loads(Path(path).read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        opts = {}
    pantry = [str(x) for x in opts.get("pantry") or []] or list(DEFAULT_PANTRY)
    try:
        hh, mm = str(opts.get("schedule_time") or "08:00").split(":")
        at = time(int(hh), int(mm))
    except ValueError:
        log.warning("schedule_time ongeldig, 08:00 gebruikt")
        at = DEFAULT_SCHEDULE
    return pantry, at


def load_app_version(path: Path = OPTIONS_PATH) -> str:
    """The `ah_app_version` option: the AH app version the add-on presents itself as."""
    try:
        opts = json.loads(Path(path).read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        opts = {}
    v = str(opts.get("ah_app_version") or "").strip() if isinstance(opts, dict) else ""
    if not v:
        return APP_VERSION
    if not all(part.isdigit() for part in v.split(".")):
        log.warning("ah_app_version ongeldig, %s gebruikt", APP_VERSION)
        return APP_VERSION
    return v


def _hhmm(value, default: time) -> time:
    try:
        hh, mm = str(value).split(":")
        return time(int(hh), int(mm))
    except (ValueError, TypeError):
        return default


def load_notify_options(path: Path = OPTIONS_PATH) -> tuple[str, list[str], time]:
    """notify_owner, notify_today and dinner_time (v0.8). A target that is not a plain
    notify service name is left out (the owner falls back to the default)."""
    try:
        opts = json.loads(Path(path).read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        opts = {}
    if not isinstance(opts, dict):
        opts = {}
    owner = notify_target(opts.get("notify_owner")) or DEFAULT_OWNER
    raw = opts.get("notify_today", DEFAULT_TODAY)
    today = [t for t in (notify_target(x) for x in (raw if isinstance(raw, list) else []))
             if t]
    dinner = _hhmm(opts.get("dinner_time") or "18:30", DEFAULT_DINNER)
    return owner, today, dinner


def ingress_url() -> str:
    """The sidebar page: /hassio/ingress/<full slug>.

    The container hostname is the full slug with "_" written as "-"
    (e.g. a repository add-on "1a2b3c4d_weekmenu" runs as "1a2b3c4d-weekmenu").
    """
    host = os.environ.get("HOSTNAME", "")
    slug = host.replace("-", "_") if host.endswith("weekmenu") else "weekmenu"
    return f"/hassio/ingress/{slug}"


async def amain() -> None:
    from aiohttp import web

    from app.web import make_app

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    from app.diag import install_ring

    install_ring()  # the last log lines for api/diagnose
    pantry, at = load_options()
    version = load_app_version()
    owner, today_targets, dinner = load_notify_options()
    store = TokenStore(DATA / "tokens.json")
    async with httpx.AsyncClient(timeout=30) as ah_http, \
            httpx.AsyncClient(timeout=15) as ha_http:
        ctx = Context(
            client=AhClient(store, ah_http, app_version=version),
            app_version=version,
            ledger=Ledger(DATA / "weeks"),
            hass=Hass(ha_http, owner=owner, today=today_targets),
            store=store,
            pantry=pantry,
            total_path=TOTAL_PATH,
            state_path=DATA / "state.json",
            folder_path=DATA / "folder.json",
            schedule_time=at,
            dinner_time=dinner,
            ingress_url=ingress_url(),
        )
        load_state(ctx)
        ctx.bonus_cache = load_folders(ctx.folder_path)
        scan_too_new(ctx)  # the message goes out with the first look of the schedule
        # no access log: its Referer carries the ingress token, and it adds nothing
        runner = web.AppRunner(make_app(ctx, allowed_remotes={INGRESS_PROXY}),
                               access_log=None)
        await runner.setup()
        await web.TCPSite(runner, "0.0.0.0", PORT).start()
        log.info("Weekmenu AH gestart, kijkt elke dag vanaf %s elk uur of de folder er is",
                 at.strftime("%H:%M"))
        await update_total(ctx, ctx.clock().date())  # publish_total from /data/totaal.json
        stdin_task = asyncio.create_task(stdin_loop(ctx))
        warm = asyncio.create_task(warm_folder(ctx))
        ctx.tasks.add(warm)
        warm.add_done_callback(ctx.tasks.discard)
        try:
            await schedule_loop(ctx)
        finally:
            stdin_task.cancel()
            await runner.cleanup()


def main() -> None:
    asyncio.run(amain())


if __name__ == "__main__":
    main()
