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
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Callable, Literal
from zoneinfo import ZoneInfo

import httpx

from app.ahclient import AhApiError, AhClient, AhLoginExpired, BonusPeriod, Product
from app.hass import (
    TOTAL_PATH,
    Hass,
    RawSettings,
    format_message,
    load_total_state,
    plan_view,
)
from app.ledger import Ledger, WeekLog
from app.planner import (
    Plan,
    Settings,
    build_plan,
    dish_from_dict,
    dish_to_dict,
    put_dish,
    rescale_dish,
    restore_dish,
    swap_dish,
)
from app import staples
from app.durable import fsync_dir
from app.tokens import TokenStore
from app.week import (
    WEEKDAGEN,
    away_dates,
    evenings,
    lange_datum,
    menu_week_start,
)
from app.writer import WriteResult, apply

# swap_dish, rescale_dish and restore_dish are looked up here by the web API
__all__ = ["run_round", "swap_dish", "rescale_dish", "restore_dish", "put_dish", "build_plan",
           "dish_to_dict", "dish_from_dict"]

log = logging.getLogger("weekmenu")

Mode = Literal["schema", "nu_maken", "nieuw_menu"]
MODES = ("schema", "nu_maken", "nieuw_menu")
BUTTONS = ("nu_maken", "nieuw_menu")

DATA = Path("/data")
OPTIONS_PATH = DATA / "options.json"
DEFAULT_PANTRY = ["olie", "zout", "peper", "suiker", "bloem", "azijn"]
DEFAULT_SCHEDULE = time(8, 0)
LAST_CHECK = time(18, 0)  # the last look of the day; on the Sunday the fallback
LAST_FALLBACK_TRY = time(23, 0)  # on the fallback Sunday: keep trying a failing round
FALLBACK_DAYS = 3  # the fallback is on the Sunday, three days before the menu Wednesday
PERIOD_WARNING = (
    "De folder van volgende week was er nog niet: "
    "prijzen gelden woensdag waarschijnlijk niet meer"
)
LOGIN_EXPIRED = "AH-login verlopen, log opnieuw in"
LOGIN_EXPIRED_COMPLETION = ("AH-login verlopen. Log opnieuw in via de Weekmenu-app; "
                            "daarna wordt het menu vanzelf afgemaakt.")
FINAL_FAILURE = ("Weekmenu kon niet gemaakt worden: Home Assistant/AH niet bereikbaar. "
                 "Tik op Nu maken in de Weekmenu-app.")
ALL_AWAY = ("Alle avonden van deze week staan op niet thuis (0 personen). "
            "Er is geen menu gemaakt.")
WRITE_FAILED_HINT = "Tik op Nu maken in de Weekmenu-app om de rest toe te voegen."
STAPLES_TIMEOUT = 60  # seconds for the whole purchase-history step before the message
PORT = 8099
INGRESS_PROXY = "172.30.32.2"


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
    ingress_url: str = "/hassio/ingress/weekmenu"
    clock: Callable[[], datetime] = local_now
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # status for api/status
    last_round: str | None = None
    error: str | None = None
    login_expired: bool = False
    busy: bool = False  # a round started from the web API, not finished yet
    # schedule: the last hourly slot looked at, and the folder that is not out yet
    checked: datetime | None = None
    waiting: Waiting | None = None
    error_kind: str | None = None  # "read": a read that failed; a waiting result clears it
    button_notified: date | None = None  # menu week of the last "folder not out" push
    away_notified: date | None = None  # menu week of the last "all evenings away" push
    total_day: date | None = None
    # bonus offers per period start: the folder is the same all week
    bonus_cache: dict[date, list[Product]] = field(default_factory=dict)
    tasks: set = field(default_factory=set)


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


def short_error(e: BaseException) -> str:
    """A description without secrets: AhClient errors never carry bodies or tokens."""
    if isinstance(e, AhApiError):
        return str(e)[:200]
    return type(e).__name__


def week_of(day: date) -> date:
    """The Wednesday that starts the menu week holding `day`."""
    return day - timedelta(days=(day.weekday() - 2) % 7)


def make_settings(ctx: Context, raw: RawSettings, start: date, exclude=()) -> Settings:
    aantal = min(7, max(1, raw.aantal))
    days = evenings(start, aantal, raw.away)
    persons = {d: max(1, raw.persons.get(WEEKDAGEN[d.weekday()], 2)) for d in days}
    recent = ctx.ledger.recent_recipe_ids(start) | set(exclude)
    return Settings(days, persons, list(ctx.pantry), recent)


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
    """The offers of the period holding the menu Wednesday.

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
        ctx.bonus_cache = {period.start: await ctx.client.bonus_offers(period)}
    return ctx.bonus_cache[period.start], warning


# --- the round --------------------------------------------------------------------------


async def run_round(ctx: Context, now: datetime, mode: Mode) -> RoundOutcome:
    """One round for the next menu week.

    "schema" is the automatic hourly look: it does nothing once the log has a menu
    for that week, and before the Sunday 18:00 fallback a failure that wrote nothing
    is left for the next hour, without a message. Buttons never use the old folder
    before that fallback moment either: they report waiting_for_folder.
    """
    start = menu_week_start(now)
    fallback = fallback_due(ctx, now, start)
    try:
        raw = await ctx.hass.read_settings()
    except Exception as e:  # not only HTTP: nothing may stop the schedule silently
        msg = f"Home Assistant-instellingen niet te lezen ({type(e).__name__})"
        ctx.error, ctx.error_kind = msg, "read"
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
        outcome = await _round(ctx, now, mode, start, raw, fallback)
    if outcome.status in ("done", "error", "login_expired"):
        ctx.last_round = now.isoformat(timespec="minutes")
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
        if mode == "schema" and auto_done(wlog):
            return RoundOutcome("skipped", start)  # the log says done: never again
        if mode == "schema" and wlog.plan:
            # a round stopped halfway (crash or write error): complete it once, from the
            # log (pending is settled first, so no write is ever repeated blind)
            completing = True
            wlog.completion_tried = True
            ctx.ledger.save(wlog)
            log.info("Ronde voor week %s was niet afgemaakt: één keer aanvullen", start)
        if mode != "nieuw_menu" and wlog.plan:
            reused = True
            plan, replaced = Plan.from_dict(wlog.plan), None  # same menu, only fill gaps
        else:
            if not make_settings(ctx, raw, start).evenings:  # nobody eats at home: no menu
                log.info("Alle avonden van de week van %s zijn niet thuis: geen menu", start)
                if mode != "schema" or ctx.away_notified != start:
                    ctx.away_notified = start
                    await guard(ctx.hass.notify("Weekmenu", ALL_AWAY, ctx.ingress_url), "melding")
                return RoundOutcome("skipped", start)
            got = await bonus_for_week(ctx, start, now, fallback=fallback)
            if isinstance(got, Waiting):
                log.info("Folder voor de week van %s nog niet uit (verwacht %s)",
                         start, got.expected or "onbekend")
                return RoundOutcome("waiting_for_folder", start, expected=got.expected)
            bonus, warning = got
            replaced = Plan.from_dict(wlog.plan) if wlog.plan else None
            old_ids = {d.recipe.id for d in replaced.dishes} if replaced else set()
            plan = await build_plan(ctx.client, bonus, make_settings(ctx, raw, start, old_ids))
            if not plan.dishes:
                raise AhApiError("geen enkel recept gevonden dat in de regels past")
            wlog.period_warning = warning  # a menu from the right folder clears it
            if replaced is not None:  # a new menu: the swap history was about the old one
                wlog.swapped_out.clear()
                wlog.previous.clear()
                wlog.previous_dish.clear()
            # a new menu is a new round: apply saves these with the plan, before writing,
            # so a crash halfway is completed by the next automatic slot
            wlog.round_done = wlog.completion_tried = wlog.notified = False
        result = await apply(ctx.client, ctx.ledger, wlog, plan, replaced)
    except AhLoginExpired:
        known = ctx.login_expired
        ctx.login_expired = True
        log.warning("AH-login verlopen")
        if completing:  # not a real try: after logging in again the next slot completes
            wlog.completion_tried = False
            try:
                ctx.ledger.save(wlog)
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
        text = (f"Er ging iets mis: {msg}. Er is verder niets toegevoegd; "
                "wat al gelukt was staat in het logboek.")
        if written:
            text += " " + WRITE_FAILED_HINT
        await guard(ctx.hass.notify("Weekmenu mislukt", text, ctx.ingress_url), "melding")
        return RoundOutcome("error", start, error=msg)

    # before the message, which names the count; bounded so the message is never late
    await refresh_staples(ctx, wlog, plan, start, now)
    recount_extras(ctx, wlog)  # apply may have settled a staple write
    await guard(ctx.hass.publish(plan_view(plan, start, result.target,
                                          away_dates(start, raw.away)),
                                 plan.savings + wlog.extras_savings), "sensoren")
    nothing_new = not (result.added or result.pending or result.unknown)
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
                ctx.ledger.save(wlog)
            except Exception as e:
                log.warning("Logboek %s niet bijgewerkt (%s)", start, type(e).__name__)
    if not wlog.round_done:
        wlog.round_done = True
        try:
            ctx.ledger.save(wlog)
        except Exception as e:
            log.warning("Logboek %s niet bijgewerkt (%s)", wlog.week, type(e).__name__)
    return RoundOutcome("done", start, warning=warning, plan=plan, result=result)


def recount_extras(ctx: Context, wlog: WeekLog) -> None:
    """extras_savings from the staples in extras_added (a settle can add one)."""
    new = staples.extras_savings(wlog)
    if new != wlog.extras_savings:
        wlog.extras_savings = new
        try:
            ctx.ledger.save(wlog)
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
        ctx.ledger.save(wlog)
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
        weeks = ctx.ledger.weeks()
        value = {w.week.isoformat(): round(w.savings + w.extras_savings, 2) for w in weeks}
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
        except Exception as e:
            log.error("Totaal niet opgeslagen: %s", type(e).__name__)
            return
        ctx.total_day = today
        for w in new:  # display only; the file above decides
            if not w.counted:
                w.counted = True
                try:
                    ctx.ledger.save(w)
                except Exception as e:
                    log.warning("Logboek %s niet bijgewerkt (%s)", w.week, type(e).__name__)


# --- schedule, state and stdin ----------------------------------------------------------


def load_state(ctx: Context) -> None:
    """state.json: {"checked": ISO datetime, "waiting": {"week", "expected"}}.

    An older file ({"schedule_done": ...}) or a broken one loads as empty: the log
    still knows which weeks have a menu, so nothing runs twice.
    """
    if ctx.state_path is None:
        return
    try:
        data = json.loads(Path(ctx.state_path).read_text(encoding="utf-8"))
        checked = data.get("checked")
        waiting = data.get("waiting")
        notified = data.get("button_notified")
        ctx.checked = datetime.fromisoformat(checked) if checked else None
        ctx.button_notified = date.fromisoformat(notified) if notified else None
        if isinstance(waiting, dict):
            exp = waiting.get("expected")
            ctx.waiting = Waiting(date.fromisoformat(waiting["week"]),
                                  date.fromisoformat(exp) if exp else None)
    except (FileNotFoundError, KeyError, ValueError, TypeError, AttributeError):
        pass


def _save_state(ctx: Context) -> None:
    if ctx.state_path is None:
        return
    w = ctx.waiting
    data = {
        "checked": ctx.checked.isoformat(timespec="minutes") if ctx.checked else None,
        "waiting": None if w is None else {
            "week": w.week.isoformat(),
            "expected": w.expected.isoformat() if w.expected else None},
        "button_notified": (ctx.button_notified.isoformat()
                            if ctx.button_notified else None),
    }
    path = Path(ctx.state_path)
    tmp = Path(str(path) + ".tmp")
    try:
        tmp.write_text(json.dumps(data), encoding="utf-8")
        os.replace(tmp, path)
        fsync_dir(path.parent)
    except OSError as e:
        log.warning("Status niet opgeslagen (%s)", type(e).__name__)


async def tick(ctx: Context, now: datetime) -> None:
    """One look at the clock: the total once a day, and the hourly look for the menu.

    At most one look per slot, also over a restart (the slot is saved after the
    round). A menu in the log for the next menu week ends the looking for that week.
    """
    if ctx.total_day != now.date():
        await update_total(ctx, now.date())
    start = menu_week_start(now)
    last = LAST_FALLBACK_TRY if is_fallback_day(now.date(), start) else LAST_CHECK
    slot = check_slot(now, ctx.schedule_time, last)
    if slot is None or slot == ctx.checked:
        return
    ctx.checked = slot
    if not auto_done(ctx.ledger.load(menu_week_start(now))):
        await run_round(ctx, now, "schema")
    _save_state(ctx)  # after the round: a crash halfway looks again after the restart


async def schedule_loop(ctx: Context) -> None:
    while True:
        now = ctx.clock()
        try:
            await tick(ctx, now)
        except Exception as e:
            log.error("Schema: %s", short_error(e))
        await asyncio.sleep(60 - ctx.clock().second)


async def handle_command(ctx: Context, line: str) -> RoundOutcome | None:
    """One stdin line: {"command": "nu_maken"} or {"command": "nieuw_menu"}."""
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
    out = await run_round(ctx, ctx.clock(), cmd)
    if out.status == "waiting_for_folder" and ctx.button_notified != out.week:
        # pressed in Home Assistant, where the app's notice is not seen: say it once
        ctx.button_notified = out.week
        _save_state(ctx)
        when = f", verwacht vanaf {lange_datum(out.expected)}" if out.expected else ""
        await guard(ctx.hass.notify(
            "Weekmenu",
            f"De folder van volgende week is er nog niet{when}. Het menu komt dan vanzelf.",
            ctx.ingress_url), "melding")
    return out


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
    pantry, at = load_options()
    store = TokenStore(DATA / "tokens.json")
    async with httpx.AsyncClient(timeout=30) as ah_http, \
            httpx.AsyncClient(timeout=15) as ha_http:
        ctx = Context(
            client=AhClient(store, ah_http),
            ledger=Ledger(DATA / "weeks"),
            hass=Hass(ha_http),
            store=store,
            pantry=pantry,
            total_path=TOTAL_PATH,
            state_path=DATA / "state.json",
            schedule_time=at,
            ingress_url=ingress_url(),
        )
        load_state(ctx)
        runner = web.AppRunner(make_app(ctx, allowed_remotes={INGRESS_PROXY}))
        await runner.setup()
        await web.TCPSite(runner, "0.0.0.0", PORT).start()
        log.info("Weekmenu AH gestart, kijkt elke dag vanaf %s elk uur of de folder er is",
                 at.strftime("%H:%M"))
        await update_total(ctx, ctx.clock().date())  # publish_total from /data/totaal.json
        stdin_task = asyncio.create_task(stdin_loop(ctx))
        try:
            await schedule_loop(ctx)
        finally:
            stdin_task.cancel()
            await runner.cleanup()


def main() -> None:
    asyncio.run(amain())


if __name__ == "__main__":
    main()
