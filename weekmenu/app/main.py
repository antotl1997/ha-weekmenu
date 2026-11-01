"""The add-on process: one round tied together, the Sunday schedule, stdin commands.

A round: read the helpers, take the bonus period of the menu Wednesday, build the
whole menu (or reuse the week's menu from the log), then write the delta to AH,
publish the sensors and send the push message.

Every path that writes to AH (the rounds and the write calls of the web API) runs
under `Context.lock`: AhClient has no refresh lock, and two writers at once could
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
from app.hass import TOTAL_PATH, Hass, RawSettings, format_message, load_total, plan_view
from app.ledger import Ledger, WeekLog
from app.planner import Plan, Settings, build_plan, rescale_dish, restore_dish, swap_dish
from app.tokens import TokenStore
from app.week import GEEN_OUDERAVOND, WEEKDAGEN, evenings, menu_week_start
from app.writer import WriteResult, apply

# swap_dish, rescale_dish and restore_dish are looked up here by the web API
__all__ = ["run_round", "swap_dish", "rescale_dish", "restore_dish", "build_plan"]

log = logging.getLogger("weekmenu")

Mode = Literal["schema", "nu_maken", "nieuw_menu"]
MODES = ("schema", "nu_maken", "nieuw_menu")
BUTTONS = ("nu_maken", "nieuw_menu")

DATA = Path("/data")
OPTIONS_PATH = DATA / "options.json"
DEFAULT_PANTRY = ["olie", "zout", "peper", "suiker", "bloem", "azijn"]
DEFAULT_SCHEDULE = time(8, 0)
LAST_RETRY = time(18, 0)  # the last hourly try for next week's folder
PERIOD_WARNING = (
    "De folder van volgende week was er nog niet: "
    "prijzen gelden woensdag waarschijnlijk niet meer"
)
LOGIN_EXPIRED = "AH-login verlopen, log opnieuw in"
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
    # schedule
    retry_at: datetime | None = None
    schedule_done: date | None = None
    total_day: date | None = None
    # bonus offers per period start: the folder is the same all week
    bonus_cache: dict[date, list[Product]] = field(default_factory=dict)
    tasks: set = field(default_factory=set)


@dataclass
class RoundOutcome:
    status: Literal["done", "skipped", "retry", "login_expired", "error"]
    week: date | None = None
    retry_at: datetime | None = None
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
    ouders = raw.ouders if raw.ouders in WEEKDAGEN else GEEN_OUDERAVOND
    days = evenings(start, aantal, ouders)
    persons = {d: max(1, raw.persons.get(WEEKDAGEN[d.weekday()], 2)) for d in days}
    recent = ctx.ledger.recent_recipe_ids(start) | set(exclude)
    return Settings(days, persons, list(ctx.pantry), recent)


def _period_for(periods: list[BonusPeriod], day: date) -> BonusPeriod | None:
    return next((p for p in periods if p.start <= day <= p.end), None)


async def bonus_for_week(
    ctx: Context, start: date, now: datetime, may_retry: bool
) -> tuple[list[Product], str | None] | datetime:
    """The offers of the period holding the menu Wednesday.

    Missing and `may_retry` (the schedule): the next full hour while that is <= 18:00.
    Otherwise fall back to the current period, with the warning for the message.
    """
    periods = await ctx.client.bonus_periods()
    period = _period_for(periods, start)
    warning = None
    if period is None:
        nxt = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        if may_retry and nxt.date() == now.date() and nxt.time() <= LAST_RETRY:
            return nxt
        period = _period_for(periods, now.date())
        if period is None:
            raise AhApiError("geen bonusfolder gevonden voor deze week")
        warning = PERIOD_WARNING
    if period.start not in ctx.bonus_cache:
        ctx.bonus_cache = {period.start: await ctx.client.bonus_offers(period)}
    return ctx.bonus_cache[period.start], warning


# --- the round --------------------------------------------------------------------------


async def run_round(ctx: Context, now: datetime, mode: Mode) -> RoundOutcome:
    start = menu_week_start(now)
    try:
        raw = await ctx.hass.read_settings()
    except httpx.HTTPError as e:
        msg = f"Home Assistant-instellingen niet te lezen ({type(e).__name__})"
        log.warning(msg)
        ctx.error = msg
        return RoundOutcome("error", start, error=msg)
    if mode == "schema" and not raw.enabled:
        log.info("Weekmenu staat uit: geen zondagronde")
        return RoundOutcome("skipped", start)

    async with ctx.lock:
        outcome = await _round(ctx, now, mode, start, raw)
    if outcome.status != "retry":
        ctx.last_round = now.isoformat(timespec="minutes")
    if outcome.status == "done":
        ctx.error = None
        ctx.login_expired = False
    elif outcome.status in ("error", "login_expired"):
        ctx.error = outcome.error
    log.info("Ronde %s voor week %s: %s", mode, start, outcome.status)
    await update_total(ctx, now.date())
    return outcome


async def _round(ctx: Context, now: datetime, mode: Mode, start: date, raw: RawSettings):
    warning = None
    try:
        wlog = ctx.ledger.load(start)
        if mode != "nieuw_menu" and wlog.plan:
            plan, replaced = Plan.from_dict(wlog.plan), None  # same menu, only fill gaps
        else:
            got = await bonus_for_week(ctx, start, now, may_retry=(mode == "schema"))
            if isinstance(got, datetime):
                log.info("Folder van volgende week nog niet zichtbaar, opnieuw om %s", got)
                return RoundOutcome("retry", start, retry_at=got)
            bonus, warning = got
            replaced = Plan.from_dict(wlog.plan) if wlog.plan else None
            old_ids = {d.recipe.id for d in replaced.dishes} if replaced else set()
            plan = await build_plan(ctx.client, bonus, make_settings(ctx, raw, start, old_ids))
            if not plan.dishes:
                raise AhApiError("geen enkel recept gevonden dat in de regels past")
            if replaced is not None:  # a new menu: the swap history was about the old one
                wlog.swapped_out.clear()
                wlog.previous.clear()
        result = await apply(ctx.client, ctx.ledger, wlog, plan, replaced)
    except AhLoginExpired:
        ctx.login_expired = True
        log.warning("AH-login verlopen")
        await guard(ctx.hass.notify("Weekmenu", LOGIN_EXPIRED, ctx.ingress_url), "melding")
        return RoundOutcome("login_expired", start, error=LOGIN_EXPIRED)
    except Exception as e:
        msg = short_error(e)
        log.error("Ronde mislukt: %s", msg)
        await guard(ctx.hass.notify(
            "Weekmenu mislukt",
            f"Er ging iets mis: {msg}. Er is verder niets toegevoegd; "
            "wat al gelukt was staat in het logboek.",
            ctx.ingress_url,
        ), "melding")
        return RoundOutcome("error", start, error=msg)

    await guard(ctx.hass.publish(plan_view(plan, start, result.target, raw.ouders),
                                 plan.savings), "sensoren")
    title, body = format_message(plan, result, raw.ouders, warning)
    await guard(ctx.hass.notify(title, body, ctx.ingress_url), "melding")
    return RoundOutcome("done", start, warning=warning, plan=plan, result=result)


async def update_total(ctx: Context, today: date) -> None:
    """Add every week that has started and was not counted yet, then publish the total.

    The total file is written before the sensor POST (Hass.publish_total), so an HA
    error still leaves the weeks counted; a failing file write counts nothing.
    """
    async with ctx.lock:
        weeks = ctx.ledger.weeks_to_count(today)
        total = round(load_total(ctx.total_path)
                      + sum(w.savings + w.extras_savings for w in weeks), 2)
        try:
            await ctx.hass.publish_total(total)
        except httpx.HTTPError as e:
            log.warning("Home Assistant: totaal niet gezet (%s)", type(e).__name__)
        except Exception as e:
            log.error("Totaal niet opgeslagen: %s", type(e).__name__)
            return
        for w in weeks:
            w.counted = True
            ctx.ledger.save(w)
        ctx.total_day = today


# --- schedule, state and stdin ----------------------------------------------------------


def load_state(ctx: Context) -> None:
    if ctx.state_path is None:
        return
    try:
        data = json.loads(Path(ctx.state_path).read_text(encoding="utf-8"))
        ctx.schedule_done = date.fromisoformat(data["schedule_done"])
    except (FileNotFoundError, KeyError, ValueError, TypeError):
        pass


def _save_state(ctx: Context) -> None:
    if ctx.state_path is None or ctx.schedule_done is None:
        return
    try:
        path = Path(ctx.state_path)
        tmp = Path(str(path) + ".tmp")
        tmp.write_text(json.dumps({"schedule_done": ctx.schedule_done.isoformat()}),
                       encoding="utf-8")
        os.replace(tmp, path)
    except OSError as e:
        log.warning("Status niet opgeslagen (%s)", type(e).__name__)


async def tick(ctx: Context, now: datetime) -> None:
    """One look at the clock: the total once a day, the Sunday round, a pending retry."""
    if ctx.total_day != now.date():
        await update_total(ctx, now.date())
    due_sunday = (now.weekday() == 6 and now.time() >= ctx.schedule_time
                  and ctx.schedule_done != now.date())
    due_retry = ctx.retry_at is not None and now >= ctx.retry_at
    if not (due_sunday or due_retry):
        return
    if due_sunday:
        ctx.schedule_done = now.date()
    ctx.retry_at = None
    out = await run_round(ctx, now, "schema")
    ctx.retry_at = out.retry_at
    if out.status != "retry":
        _save_state(ctx)  # a restart after this does not run the Sunday round again


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
        log.info("Weekmenu AH gestart, zondagronde om %s", at.strftime("%H:%M"))
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
