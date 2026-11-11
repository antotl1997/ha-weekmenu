"""Ingress page and JSON API on port 8099.

Ingress puts `/api/hassio_ingress/<token>/` in front of every path, so the page
must use relative paths (`api/week`, not `/api/week`). Every call that may write
to AH, or refresh the AH tokens, runs under `ctx.lock`.

Never in a response or in the log: the login code, a token, an Authorization header.

`api/health` (the Supervisor watchdog) takes no lock and calls nothing. The watchdog
comes from the Supervisor, which is also the ingress proxy (172.30.32.2), so the same
allowlist lets it through; no other address is needed.
"""

from __future__ import annotations

import asyncio
import html
import logging
import re
from collections.abc import Iterable
from datetime import date, timedelta
from pathlib import Path

import httpx
from aiohttp import web

from app import main
from app.ahclient import LOGIN_URL, AhApiError, AhLoginExpired, Product, extract_code
from app.hass import (
    BUSY,
    HELPER_AAN,
    HELPER_AVONDEN,
    eat_count,
    helper_persons,
    load_total,
    plan_view,
    unsure,
)
from app.ledger import leftovers, new_folder_available
from app.planner import Dish, Plan
from app.deals import qty_for_deal, savings as deal_savings
from app.diag import diagnose
from app.durable import UPDATE_MESSAGE, SchemaTooNew
from app.health import PAUSED_NOTE
from app.staples import extras_savings, staple_list
from app.week import (
    WEEKDAGEN,
    away_dates,
    bestelling_zin,
    eat_days,
    menu_week_start,
)
from app.writer import OFFER, apply, apply_extras, held_note, manual_note, missing

log = logging.getLogger("weekmenu.web")

STATIC_DIR = Path(__file__).parent / "static"
# the user guide: weekmenu/GIDS.md here, /GIDS.md in the image (see Dockerfile)
GUIDE_PATH = Path(__file__).resolve().parent.parent / "GIDS.md"
_ASSET = r"{name:[A-Za-z0-9_-][A-Za-z0-9_.-]*\.(?:css|js|png|svg|ico|jpg|webp|json|webmanifest)}"
MIN_PERSONS, MAX_PERSONS = 1, 8
MAX_PERSONS_LEFTOVERS = 2 * MAX_PERSONS  # a dish that also feeds a leftover night

FALLBACK_HTML = """<!doctype html>
<html lang="nl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Weekmenu</title>
<style>
body{font-family:system-ui,sans-serif;background:#f4f1ec;margin:0;padding:24px;color:#222}
main{max-width:480px;margin:0 auto;background:#fff;border-radius:16px;padding:20px}
textarea{width:100%;box-sizing:border-box;min-height:90px;font:inherit}
button{background:#2f6b3a;color:#fff;border:0;border-radius:10px;padding:12px 18px;font:inherit}
</style></head><body><main>
<h1>Weekmenu AH</h1>
<p id="status">Status laden…</p>
<h2>Inloggen bij AH</h2>
<p>Op een computer:</p>
<ol>
<li>Open deze link: <a id="login" target="_blank" rel="noopener">AH-inlogpagina</a>.</li>
<li>Druk F12 en kies Console.</li>
<li>Log in.</li>
<li>Kopieer de regel die begint met <code>appie://login-exit?code=</code></li>
<li>Plak hem hier (dat kan ook op je telefoon, bijvoorbeeld via WhatsApp naar jezelf).</li>
</ol>
<textarea id="code" placeholder="appie://login-exit?code=…"></textarea>
<p><button id="send">Inloggen</button></p>
<p id="msg"></p>
</main>
<script>
const $ = (id) => document.getElementById(id);
async function status() {
  const s = await (await fetch("api/status")).json();
  $("status").textContent = (s.logged_in ? "Ingelogd bij AH." : "Niet ingelogd bij AH.")
    + (s.last_round ? " Laatste menu: " + s.last_round + "." : "")
    + (s.error ? " Fout: " + s.error : "");
  $("login").href = s.login_url;
}
$("send").onclick = async () => {
  $("send").disabled = true;
  const r = await fetch("api/login", {method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({code: $("code").value})});
  const b = await r.json();
  $("code").value = "";
  $("msg").textContent = r.ok ? "Ingelogd." : (b.error || "Mislukt.");
  $("send").disabled = false;
  status();
};
status();
</script></body></html>
"""


GUIDE_HTML = """<!doctype html>
<html lang="nl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Weekmenu: de gids</title>
<style>
body{font-family:"Segoe UI",Roboto,system-ui,sans-serif;background:#f4f1ec;margin:0;color:#22201c}
main{max-width:480px;margin:0 auto;padding:22px 20px 40px;line-height:1.55;font-size:15px}
h1{font-size:24px;margin:0 0 10px}h2{font-size:18px;margin:22px 0 6px}
ul,ol{padding-left:22px}li{margin:4px 0}
a{color:#2f6b3a;display:inline-block;min-height:44px;line-height:44px}
code{font-family:ui-monospace,Consolas,monospace;font-size:13px;background:#ebe7e0;border-radius:6px;padding:1px 5px}
</style></head><body><main>
<a href="./">Terug naar Weekmenu</a>
{body}
</main></body></html>
"""


def _inline(text: str) -> str:
    """Escaped first; then only **bold** and `code`."""
    out = html.escape(text, quote=True)
    out = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", out)
    return re.sub(r"`(.+?)`", r"<code>\1</code>", out)


def markdown_page(md: str) -> str:
    """The small part of Markdown GIDS.md uses: headings, paragraphs and lists."""
    parts: list[str] = []
    para: list[str] = []
    items: list[str] = []
    kind = ""

    def flush():
        nonlocal kind
        if para:
            parts.append(f"<p>{_inline(' '.join(para))}</p>")
            para.clear()
        if items:
            parts.append(f"<{kind}>" + "".join(f"<li>{_inline(i)}</li>" for i in items)
                         + f"</{kind}>")
            items.clear()

    for line in md.splitlines():
        line = line.strip()
        m = re.match(r"^(#{1,3}) (.+)$", line)
        if not line:
            flush()
        elif m:
            flush()
            level = len(m.group(1))
            parts.append(f"<h{level}>{_inline(m.group(2))}</h{level}>")
        elif re.match(r"^[-*] ", line) or re.match(r"^\d+\. ", line):
            new = "ul" if line[0] in "-*" else "ol"
            if para or (items and new != kind):
                flush()
            kind = new
            items.append(re.sub(r"^([-*]|\d+\.) ", "", line))
        else:
            if items:
                flush()
            para.append(line)
    flush()
    return GUIDE_HTML.replace("{body}", "\n".join(parts))


# --- views ------------------------------------------------------------------------------


def _printable(text, limit: int = 80) -> str:
    """For a log line: no line breaks or other control characters, and not too long."""
    out = "".join(c if c.isprintable() else "?" for c in str(text or "?"))
    return out[:limit] + ("..." if len(out) > limit else "")


def _err(status: int, message: str, **extra) -> web.Response:
    return web.json_response({"error": message, **extra}, status=status)


def _recipe_view(d: Dish) -> dict:
    return {
        "id": d.recipe.id, "title": d.recipe.title, "image": d.recipe.image,
        "cook": d.cook, "oven": d.oven, "wait": d.wait, "cuisine": d.cuisine,
        "url": d.recipe.url,
    }


def _title(plan: Plan, pid: int) -> str:
    p = plan.products.get(pid)
    return p.title if p else str(pid)


def _leftovers(log, plan: Plan) -> list[dict]:
    return [{"product": _title(plan, p), "qty": n} for p, n in leftovers(log, plan).items()]


def _held(wlog, plan: Plan | None, start: date, retry: bool) -> str | None:
    """The note of a held menu: a promise only for the week the schedule retries."""
    if not wlog.held or plan is None:
        return None
    if retry:
        return held_note(start)
    todo = missing(wlog, plan)
    return manual_note(_title(plan, p) for p in todo) if todo else None


def week_view(wlog, plan: Plan | None, start: date,
              away: Iterable[str] = (), retry: bool = True) -> dict:
    away = set(away)
    out = {
        "week": start.isoformat(), "bestelling": bestelling_zin(start),
        "target": wlog.target, "days": [],
        "niet_thuis": [d.isoformat() for d in away_dates(start, away)],
        "geen_avonden": not eat_days(start, away),
        "savings": 0.0, "products": 0, "bonus_products": 0, "leftovers": [],
        "not_found": [], "short_by": 0, "pending": [], "unknown": [],
        "budget_exhausted": False, "period_warning": wlog.period_warning,
        "extras_savings": wlog.extras_savings, "offers_savings": wlog.offers_savings,
        "staples_error": wlog.staples_error,
        # Ruling 42: the menu is saved but waits for an order or list for this week
        "held": _held(wlog, Plan.from_dict(wlog.plan) if wlog.plan else None, start, retry),
    }
    if plan is None:
        return out
    by_day = {d.date: d for d in plan.dishes}
    rest_of = {d.leftover_date: d for d in plan.dishes if d.leftover_date}
    removed = set(plan.removed_days)
    out["niet_thuis"] = [i for i in out["niet_thuis"]
                         if date.fromisoformat(i) not in by_day
                         and date.fromisoformat(i) not in rest_of
                         and date.fromisoformat(i) not in removed]
    for i in range(7):
        day = date.fromordinal(start.toordinal() + i)
        dag = WEEKDAGEN[day.weekday()]
        d = by_day.get(day)
        if d is not None:
            out["days"].append({"date": day.isoformat(), "dag": dag,
                                "niet_thuis": False, "persons": d.persons, "recipe": _recipe_view(d),
                                "savings": d.savings})
        elif day in rest_of:  # "<Dag> · restjes van <gerecht>": a tap opens that dish
            src = rest_of[day]
            out["days"].append({"date": day.isoformat(), "dag": dag, "niet_thuis": False,
                                "restjes": True, "persons": 0, "recipe": None,
                                "restjes_van": {"date": src.date.isoformat(),
                                                "title": src.recipe.title},
                                "savings": 0.0})
        elif day in removed:
            out["days"].append({"date": day.isoformat(), "dag": dag,
                                "niet_thuis": False, "geen_gerecht": True, "persons": 0,
                                "recipe": None, "savings": 0.0})
        elif dag in away:
            out["days"].append({"date": day.isoformat(), "dag": dag,
                                "niet_thuis": True, "persons": 0, "recipe": None,
                                "savings": 0.0})
        else:  # an eat day with no dish and no leftovers: "geen kookmoment"
            out["days"].append({"date": day.isoformat(), "dag": dag, "niet_thuis": False,
                                "geen_kookmoment": True, "persons": 0, "recipe": None,
                                "savings": 0.0})
    out.update(
        savings=plan.savings,
        products=len(plan.basket),
        bonus_products=sum(1 for p in plan.basket if plan.products[p].is_bonus),
        leftovers=_leftovers(wlog, plan),
        not_found=list(plan.not_found),
        short_by=plan.short_by,
        budget_exhausted=plan.budget_exhausted,
        pending=[_title(plan, p) for p in wlog.pending],
        unknown=[_title(plan, p) for p in wlog.unknown],
    )
    return out


def staples_view(wlog) -> list[dict]:
    """Vaste boodschappen in de bonus, as the app lists them under the dishes."""
    return [
        {"product": s.product.title, "product_id": s.product.id, "times": s.times,
         "qty": s.qty, "mechanism": s.product.mechanism, "savings": s.savings,
         "added": s.product.id in wlog.extras_added or s.product.id in wlog.offers_added}
        for s in staple_list(wlog)
    ]


OTHER_CATEGORY = "Overig"
OFFERS_FALLBACK_NOTE = ("Dit is de huidige folder; de folder van je bezorgweek "
                        "is er nog niet.")


def offer_item(p: Product, wlog) -> dict:
    """One bonus product for the Aanbiedingen tab. added_qty: what a staple or a loose
    offer already put on the list or in the order this week."""
    return {
        "id": p.id, "title": p.title, "mechanism": p.mechanism, "price_now": p.now,
        "price_regular": p.regular, "unit_size": p.unit_size, "image": p.image,
        "added_qty": wlog.offers_added.get(p.id, 0) or wlog.extras_added.get(p.id, 0),
        "orderable": p.orderable,
    }


def offers_view(products: Iterable[Product], wlog, q: str = "", category: str = "") -> dict:
    """The folder per category (A to Z, "Overig" last), searchable on title."""
    q, category = q.strip().casefold(), category.strip().casefold()
    groups: dict[str, list[Product]] = {}
    for p in products:
        name = p.category or OTHER_CATEGORY
        if category and name.casefold() != category:
            continue
        if q and q not in p.title.casefold():
            continue
        groups.setdefault(name, []).append(p)
    names = sorted(groups, key=lambda n: (n == OTHER_CATEGORY, n.casefold()))
    return {"categories": [
        {"name": n, "items": [offer_item(p, wlog)
                              for p in sorted(groups[n], key=lambda x: x.title.casefold())]}
        for n in names]}


def dish_view(wlog, plan: Plan, d: Dish, today: date) -> dict:
    lines = [
        {"product": ln.product.title, "product_id": ln.product.id, "qty": ln.qty,
         "mechanism": ln.product.mechanism if ln.product.is_bonus else None,
         "bonus": ln.product.is_bonus, "ingredient": ln.from_ingredient, "pantry": False}
        for ln in d.lines
    ]
    lines += [
        {"product": text, "product_id": None, "qty": 0, "mechanism": None, "bonus": False,
         "ingredient": text, "pantry": True}
        for text in d.pantry
    ]
    iso = d.date.isoformat()
    return {
        "date": iso, "dag": WEEKDAGEN[d.date.weekday()], "week": wlog.week.isoformat(),
        "persons": d.persons, "savings": d.savings, "recipe": _recipe_view(d),
        "restjes": d.leftover_date.isoformat() if d.leftover_date else None,
        "lines": lines, "not_found": list(d.not_found), "steps": list(d.recipe.steps),
        "can_back": iso in wlog.previous and today < wlog.week,
        "week_savings": plan.savings, "leftovers": _leftovers(wlog, plan),
    }


# --- the app ----------------------------------------------------------------------------


def make_app(ctx, static_dir: Path = STATIC_DIR,
             allowed_remotes: set[str] | None = None) -> web.Application:
    static_dir = Path(static_dir)

    @web.middleware
    async def guard_mw(request: web.Request, handler):
        if allowed_remotes is not None and request.remote not in allowed_remotes:
            # remote and path only: no query, no Referer (it may carry the ingress token)
            log.warning("Verzoek geweigerd (niet via Home Assistant): %s %s",
                        _printable(request.remote), _printable(request.path))
            return _err(403, "alleen via Home Assistant")
        if request.path == "/api/health":  # the watchdog: nothing else, never blocks
            return await handler(request)
        try:
            return await handler(request)
        finally:
            # a read or write may have paused writing: the one push goes out now
            if main.health_needs_sync(ctx):
                try:
                    await main.sync_health(ctx)
                except Exception as e:  # never in the way of the answer itself
                    log.warning("Melding API-status mislukt (%s)", type(e).__name__)

    @web.middleware
    async def errors_mw(request: web.Request, handler):
        try:
            return await handler(request)
        except web.HTTPException:
            raise
        except SchemaTooNew as e:  # a file from a newer add-on: nothing was written
            main.note_too_new(ctx, e.path)
            return _err(409, UPDATE_MESSAGE)
        except AhLoginExpired:
            ctx.login_expired = True
            return _err(401, main.LOGIN_EXPIRED, login=True)
        except AhApiError as e:
            return _err(502, main.short_error(e))
        except httpx.HTTPError as e:
            return _err(502, f"Home Assistant niet bereikbaar ({type(e).__name__})")
        except Exception as e:
            log.error("API-fout: %s", type(e).__name__)
            return _err(500, type(e).__name__)

    app = web.Application(middlewares=[guard_mw, errors_mw])

    async def body_of(request) -> dict:
        try:
            data = await request.json()
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    def day_of(request) -> date:
        try:
            return date.fromisoformat(request.match_info["day"])
        except ValueError:
            raise web.HTTPBadRequest(text='{"error": "ongeldige datum"}',
                                     content_type="application/json") from None

    async def settings_now():
        try:
            return await ctx.hass.read_settings()
        except httpx.HTTPError:
            return None

    async def publish(start: date, plan: Plan, target: str, extras: float = 0.0) -> None:
        """Update sensor.weekmenu when this is the week it shows."""
        now = ctx.clock()
        nxt = menu_week_start(now)
        shown = nxt if ctx.ledger.load(nxt).plan else main.week_of(now.date())
        if start != shown:
            return
        raw = await settings_now()
        if raw is None:
            return
        view = plan_view(plan, start, target, away_dates(start, raw.away),
                         new_folder=new_folder_available(ctx.ledger.load(start)))
        view.update(main.sensor_extras(ctx, raw.away, now))
        await main.guard(ctx.hass.publish(view,
                                          plan.savings + extras), "sensoren")

    # --- pages ---

    async def index(request):
        page = static_dir / "index.html"
        if page.is_file():
            return web.FileResponse(page)
        return web.Response(text=FALLBACK_HTML, content_type="text/html")

    async def guide(request):
        """GIDS.md, the user guide, as a page under the ingress path ("gids")."""
        try:
            md = GUIDE_PATH.read_text(encoding="utf-8")
        except OSError:
            raise web.HTTPNotFound() from None
        return web.Response(text=markdown_page(md), content_type="text/html")

    async def asset(request):
        f = static_dir / request.match_info["name"]
        if static_dir.is_dir() and f.is_file() and f.parent == static_dir:
            return web.FileResponse(f)
        raise web.HTTPNotFound()

    # --- status and login ---

    def waiting_for(start: date | None = None):
        """The folder of a menu week without a menu is not out yet (or None)."""
        w = ctx.waiting
        if w is None or (start is not None and w.week != start) or ctx.ledger.load(w.week).plan:
            return None
        return w

    async def status(request):
        logged_in = ctx.store.load() is not None and not ctx.login_expired
        # the welcome screen: no login file, AH never answered and no menu yet
        first = (not logged_in and ctx.store.saved_at() is None
                 and ctx.health.last_ok is None
                 and not any(w.plan for w in ctx.ledger.weeks()))
        w = waiting_for()
        return web.json_response({
            # a file or the login is from a newer add-on: the app says "werk bij"
            # instead of showing the login screen
            "update_needed": UPDATE_MESSAGE if main.update_needed(ctx) else None,
            "ah_app_version": ctx.app_version,
            "last_ah_sync": ctx.health.last_ok,
            "writes_paused": ctx.health.writes_paused,
            "logged_in": logged_in, "last_round": ctx.last_round, "error": ctx.error,
            "first_start": first,
            # a menu round only: the hourly new-folder look also takes the lock
            "running": ctx.busy or ctx.in_round, "rounds": ctx.rounds,
            "login_url": LOGIN_URL,
            "planner_seconds": ctx.planner_seconds,
            "waiting_for_folder": None if w is None else {
                "week": w.week.isoformat(),
                "expected": w.expected.isoformat() if w.expected else None},
        })

    async def login(request):
        raw = (await body_of(request)).get("code")
        code = extract_code(raw) if isinstance(raw, str) else ""
        if not code:
            return _err(400, "Geen code gevonden. Plak de hele regel met appie://login-exit?code=…")
        if ctx.store.too_new():  # the answer could not be saved: keep the code unused
            main.note_too_new(ctx, ctx.store.path)
            return _err(409, UPDATE_MESSAGE, update=True)
        async with ctx.lock:  # a round may be refreshing the tokens right now
            try:
                await ctx.client.exchange_code(code)
            except AhApiError as e:
                log.warning("Inloggen mislukt (HTTP %s)", e.status)
                return _err(400, f"Inloggen mislukt (HTTP {e.status})" if e.status
                            else "Inloggen mislukt")
        ctx.login_expired = False
        ctx.error = None
        log.info("Ingelogd bij AH")
        return web.json_response({"logged_in": True})

    async def logout(request):
        """Uitloggen bij AH: tokens.json (and its .bak) gone, the app shows the login.
        Under the lock: a round may be refreshing the tokens right now."""
        async with ctx.lock:
            # a refresh still running (shielded, maybe from a cancelled caller) would
            # write new tokens after the clear: let it finish first
            task = getattr(ctx.client, "_refresh_task", None)
            if task is not None and not task.done():
                try:
                    await asyncio.shield(task)
                except Exception:
                    pass  # an expired login is fine here: it is about to go anyway
            await asyncio.to_thread(ctx.store.clear)
        ctx.login_expired = True
        log.info("Uitgelogd bij AH: login verwijderd")
        return web.json_response({"logged_in": False})

    # --- health and diagnose: no lock, no AH call ---

    async def health(request):
        return web.json_response({"ok": True})

    async def get_diagnose(request):
        return web.json_response(diagnose(ctx))

    async def retry_writes(request):
        """"Opnieuw proberen" in Diagnose: writing on again, by hand."""
        ctx.health.resume("knop Opnieuw proberen")
        await main.sync_health(ctx)
        return web.json_response({"writes_paused": ctx.health.writes_paused})

    # --- reading ---

    def shown_week(request) -> date:
        """?week=<iso> or the week the app shows by default (running, else the next one)."""
        now = ctx.clock()
        q = request.query.get("week")
        if q:
            try:
                return main.week_of(date.fromisoformat(q))
            except ValueError:
                raise web.HTTPBadRequest(text='{"error": "ongeldige week"}',
                                         content_type="application/json") from None
        current = main.week_of(now.date())
        return current if ctx.ledger.load(current).plan else menu_week_start(now)

    async def week(request):
        now = ctx.clock()
        start = shown_week(request)
        wlog = ctx.ledger.load(start)
        plan = Plan.from_dict(wlog.plan) if wlog.plan else None
        raw = await settings_now()
        out = week_view(wlog, plan, start,
                        raw.away if raw is not None else (),
                        retry=start == menu_week_start(now))
        out["enabled"] = raw.enabled if raw is not None else None  # None: HA unreadable
        out["writes_paused"] = PAUSED_NOTE if ctx.health.writes_paused else None
        w = waiting_for(start) if plan is None else None
        out["waiting_for_folder"] = None if w is None else {
            "expected": w.expected.isoformat() if w.expected else None}
        nxt = menu_week_start(now)
        nxt_log = wlog if nxt == start else ctx.ledger.load(nxt)
        out["next_week"] = nxt.isoformat() if nxt != start and nxt_log.plan else None
        # "Nieuwe folder is er": about the week Menu bijwerken remakes, whichever shows
        fresh = new_folder_available(nxt_log)
        out["new_folder_available"] = fresh
        out["new_folder_week"] = nxt.isoformat() if fresh else None
        # v0.8: tonight (whichever week is shown) and the AH login line
        out["vanavond"] = main.tonight(ctx, now.date(), raw.away if raw is not None else ())
        out["login"] = main.login_view(ctx, now)
        return web.json_response(out)

    def load_dish(day: date):
        wlog = ctx.ledger.load(main.week_of(day))
        if not wlog.plan:
            raise web.HTTPNotFound(text='{"error": "geen menu voor die week"}',
                                   content_type="application/json")
        plan = Plan.from_dict(wlog.plan)
        d = next((x for x in plan.dishes if x.date == day), None)
        if d is None:
            raise web.HTTPNotFound(text='{"error": "geen gerecht op die dag"}',
                                   content_type="application/json")
        return wlog, plan, d

    async def dish(request):
        wlog, plan, d = load_dish(day_of(request))
        return web.json_response(dish_view(wlog, plan, d, ctx.clock().date()))

    # --- writing: one at a time ---

    async def _write(day: date, change) -> web.Response:
        """Load, change and apply under the lock; `change` returns the new plan or a Response."""
        async with ctx.lock:
            wlog, plan, d = load_dish(day)
            new = await change(wlog, plan, d)
            if isinstance(new, web.Response):
                return new
            res = await apply(ctx.client, ctx.ledger, wlog, new, replaced=plan,
                              today=ctx.clock().date())
            settled = extras_savings(wlog)  # apply may have settled a staple write
            if settled != wlog.extras_savings:
                wlog.extras_savings = settled
                await main.save_log(ctx, wlog)
        nd = next((x for x in new.dishes if x.date == day), None)
        await publish(wlog.week, new, wlog.target or "list", wlog.extras_total)
        # removed: how many products went off the list or out of the order (opschonen)
        done = {"removed": len(res.removed), "target": res.target, "held": res.held}
        if nd is None:  # "Gerecht weghalen": no dish left to show
            return web.json_response({"date": day.isoformat(), **done})
        return web.json_response({**dish_view(wlog, new, nd, ctx.clock().date()), **done})

    async def swap(request):
        day = day_of(request)
        iso = day.isoformat()

        async def change(wlog, plan, d):
            bonus, _ = await main.bonus_for_week(ctx, wlog.week, ctx.clock(), fallback=True)
            exclude = set(wlog.swapped_out.get(iso, [])) | {d.recipe.id}
            new = await main.swap_dish(ctx.client, plan, day, bonus,
                                       main.settings_from_plan(ctx, plan, wlog.week),
                                       exclude)
            nd = next((x for x in new.dishes if x.date == day), None)
            if nd is None or nd.recipe.id == d.recipe.id:
                return _err(409, "Geen ander gerecht gevonden dat in de regels past")
            wlog.swapped_out.setdefault(iso, []).append(d.recipe.id)
            wlog.previous[iso] = d.recipe.id
            wlog.previous_dish[iso] = main.dish_to_dict(d)  # "Terug" puts exactly this back
            return new

        return await _write(day, change)

    async def back(request):
        day = day_of(request)
        iso = day.isoformat()

        async def change(wlog, plan, d):
            prev = wlog.previous.get(iso)
            if prev is None:
                return _err(409, "Er is geen vorig gerecht")
            if ctx.clock().date() >= wlog.week:
                return _err(409, "De week is al begonnen")
            saved = wlog.previous_dish.get(iso)
            if saved is not None and int(saved["recipe"]["id"]) == prev:
                new = main.put_dish(plan, main.dish_from_dict(saved))  # exact, no AH reads
            else:  # a log from before previous_dish: look the recipe up again
                bonus, _ = await main.bonus_for_week(ctx, wlog.week, ctx.clock(),
                                                     fallback=True)
                new = await main.restore_dish(ctx.client, plan, day, prev, bonus)
            if d.recipe.id not in wlog.swapped_out.setdefault(iso, []):
                wlog.swapped_out[iso].append(d.recipe.id)
            del wlog.previous[iso]
            wlog.previous_dish.pop(iso, None)
            return new

        return await _write(day, change)

    async def persons(request):
        day = day_of(request)
        n = (await body_of(request)).get("persons")
        if (not isinstance(n, int) or isinstance(n, bool)
                or not MIN_PERSONS <= n <= MAX_PERSONS_LEFTOVERS):
            return _err(400, f"personen moet tussen {MIN_PERSONS} en {MAX_PERSONS_LEFTOVERS} liggen")

        async def change(wlog, plan, d):
            top = MAX_PERSONS_LEFTOVERS if d.leftover_date else MAX_PERSONS
            if n > top:  # 2 x 8 only when the dish also feeds a leftover night
                return _err(400, f"personen moet tussen {MIN_PERSONS} en {top} liggen")
            return await main.rescale_dish(ctx.client, plan, day, n)  # only this dish

        return await _write(day, change)

    async def remove(request):
        """"Gerecht weghalen": the evening gets no dish, its own products are cleaned up."""
        day = day_of(request)
        iso = day.isoformat()

        async def change(wlog, plan, d):
            wlog.previous.pop(iso, None)
            wlog.previous_dish.pop(iso, None)
            return main.remove_dish(plan, day)

        return await _write(day, change)

    async def run(request):
        mode = (await body_of(request)).get("mode")
        if mode not in main.BUTTONS:
            return _err(400, "mode moet nu_maken, nieuw_menu of bijwerken zijn")
        if ctx.busy or ctx.in_round:
            return _err(409, BUSY)
        if mode == "bijwerken" and not new_folder_available(
                ctx.ledger.load(menu_week_start(ctx.clock()))):
            return _err(409, "Er is geen nieuwe folder om het menu mee bij te werken")
        ctx.busy = True

        async def go():
            try:
                await main.run_round(ctx, ctx.clock(), mode)
            except Exception as e:
                log.error("Ronde mislukt: %s", main.short_error(e))
            finally:
                ctx.busy = False

        task = asyncio.create_task(go())
        ctx.tasks.add(task)
        task.add_done_callback(ctx.tasks.discard)
        return web.json_response({"started": True, "mode": mode}, status=202)

    # --- vaste boodschappen in de bonus ---

    async def get_staples(request):
        return web.json_response(staples_view(ctx.ledger.load(shown_week(request))))

    async def add_staple(request):
        """One tap: the staple goes in once, through the same write path as the menu."""
        try:
            pid = int(request.match_info["pid"])
        except ValueError:
            return _err(400, "ongeldig product")
        start = shown_week(request)
        now = ctx.clock()
        if start not in (main.week_of(now.date()), menu_week_start(now)):
            return _err(400, "Alleen voor deze of de volgende menuweek")
        async with ctx.lock:
            wlog = ctx.ledger.load(start)
            staple = next((s for s in staple_list(wlog) if s.product.id == pid), None)
            if staple is None:
                return _err(404, "geen voorstel voor dit product")
            target = wlog.target
            if pid not in wlog.extras_added and pid not in wlog.offers_added:
                res = await apply_extras(ctx.client, ctx.ledger, wlog, {pid: staple.qty},
                                         {pid: staple.product.title})
                if res.held:  # the open order is for another week: nothing written
                    return _err(409, res.held, items=staples_view(wlog))
                target = res.target
                wlog.extras_savings = extras_savings(wlog)
                await main.save_log(ctx, wlog)
                if pid not in wlog.extras_added:  # outcome unsure, or unknown: never blind
                    return _err(409, f"{unsure(target)}. Controleer in de AH-app.",
                                items=staples_view(wlog))
        if wlog.plan:
            await publish(start, Plan.from_dict(wlog.plan), wlog.target or "list",
                          wlog.extras_total)
        # target: where this product went ("order" or "list"), for the app's message
        return web.json_response({"items": staples_view(wlog), "target": target or "list"})

    # --- aanbiedingen: the whole bonus folder of the menu week ---

    def check_offer_week(start: date) -> None:
        now = ctx.clock()
        if start not in (main.week_of(now.date()), menu_week_start(now)):
            raise web.HTTPBadRequest(
                text='{"error": "Alleen voor deze of de volgende menuweek"}',
                content_type="application/json")

    def cached_folder(start: date) -> tuple[list[Product], bool] | None:
        """The folder of the period holding the menu Wednesday, if it is in memory, and
        whether it is the current folder standing in for one that is not out yet."""
        for first, items in ctx.bonus_cache.items():
            if first <= start <= first + timedelta(days=6):
                return items, False
        known = ctx.offers_fallback.get(start)
        if known is not None and known[0] in ctx.bonus_cache:
            # valid for an hour, and until the hourly look has seen the real folder
            fresh = ctx.clock() - known[1] < timedelta(hours=1)
            if fresh and ctx.ledger.load(start).new_folder is None:
                return ctx.bonus_cache[known[0]], True
        return None

    async def fetch_folder(start: date) -> tuple[list[Product], bool]:
        got = await main.folder_for_week(ctx, start, ctx.clock(), fallback=True)
        items, warning, period_start = got  # fallback=True never gives Waiting
        if warning:
            ctx.offers_fallback[start] = (period_start, ctx.clock())
        else:
            ctx.offers_fallback.pop(start, None)
        return items, bool(warning)

    async def folder(start: date, locked: bool = False) -> tuple[list[Product], bool]:
        """From the cache; when empty fetched once, under the lock (like a menu round)."""
        got = cached_folder(start)
        if got is not None:
            return got
        if locked:
            return await fetch_folder(start)
        async with ctx.lock:
            got = cached_folder(start)  # another request may have filled it meanwhile
            if got is not None:
                return got
            return await fetch_folder(start)

    async def get_offers(request):
        start = shown_week(request)
        check_offer_week(start)
        products, fallback = await folder(start)
        out = offers_view(products, ctx.ledger.load(start), request.query.get("q", ""),
                          request.query.get("category", ""))
        out["period_warning"] = OFFERS_FALLBACK_NOTE if fallback else None
        return web.json_response(out)

    async def add_offer(request):
        """One tap: the deal's quantity goes in once, through the same write path as staples."""
        try:
            pid = int(request.match_info["pid"])
        except ValueError:
            return _err(400, "ongeldig product")
        start = shown_week(request)
        check_offer_week(start)
        async with ctx.lock:
            product = next((p for p in (await folder(start, locked=True))[0] if p.id == pid), None)
            if product is None:
                return _err(404, "dit product zit niet in de folder")
            if not product.orderable:
                return _err(409, "Dit product is niet te bestellen")
            wlog = ctx.ledger.load(start)
            target = wlog.target
            # a staple that is already in goes in once; its deal quantity is covered
            if pid not in wlog.offers_added and pid not in wlog.extras_added:
                qty = qty_for_deal(product, 1)
                res = await apply_extras(
                    ctx.client, ctx.ledger, wlog, {pid: qty}, {pid: product.title},
                    kind=OFFER, savings={pid: deal_savings(product, qty)})
                if res.held:  # the open order is for another week: nothing written
                    return _err(409, res.held, added_qty=0)
                target = res.target
                if pid not in wlog.offers_added:  # outcome unsure, or unknown: never blind
                    return _err(409, f"{unsure(target)}. Controleer in de AH-app.",
                                added_qty=0)
        if wlog.plan:
            await publish(start, Plan.from_dict(wlog.plan), wlog.target or "list",
                          wlog.extras_total)
        return web.json_response({
            "id": pid, "added_qty": offer_item(product, wlog)["added_qty"],
            "target": target or "list", "offers_savings": wlog.offers_savings})

    # --- savings and settings ---

    async def savings(request):
        today = ctx.clock().date()
        weeks = [w for w in ctx.ledger.weeks() if w.plan]
        month = sum(w.savings + w.extras_total for w in weeks
                    if w.counted and (w.week.year, w.week.month) == (today.year, today.month))
        return web.json_response({
            "month": round(month, 2),
            "total": round(load_total(ctx.total_path), 2),
            "weeks": [{"week": w.week.isoformat(),
                       "savings": round(w.savings + w.extras_total, 2),
                       "counted": w.counted} for w in weeks],
        })

    async def get_settings(request):
        raw = await ctx.hass.read_settings()
        return web.json_response({
            # avonden: the cooking moments, at most the eat days (read_settings bounds it)
            "enabled": raw.enabled, "avonden": raw.aantal, "eetdagen": eat_count(raw.persons),
            "persons": raw.persons, "defaulted": raw.defaulted,
            "away": [dag for dag in WEEKDAGEN[2:] + WEEKDAGEN[:2] if dag in raw.away],
            "entities": {"enabled": HELPER_AAN, "avonden": HELPER_AVONDEN,
                         "persons": helper_persons()},
        })

    async def post_settings(request):
        body = await body_of(request)
        entity, value, away = body.get("entity_id"), body.get("value"), body.get("away")
        if not isinstance(entity, str) or (value is None and not isinstance(away, bool)):
            return _err(400, "entity_id en value (of away) zijn nodig")
        by_entity = {eid: dag for dag, eid in helper_persons().items()}
        if isinstance(away, bool) and entity not in by_entity:
            return _err(400, "away hoort bij een personen-helper")
        if entity in by_entity and not isinstance(away, bool) and value == 0                 and not isinstance(value, bool):
            away = True  # 0 persons is "niet thuis", whoever asks
        try:
            if entity in by_entity and isinstance(away, bool):
                # "Niet thuis eten": the add-on remembers the number from before
                value = await ctx.hass.set_away(by_entity[entity], away)
            elif entity in by_entity and (isinstance(value, bool) or not isinstance(value, int)
                                          or not MIN_PERSONS <= value <= MAX_PERSONS):
                return _err(400, "personen moeten 1 tot 8 zijn")
            elif entity == HELPER_AVONDEN and (isinstance(value, bool)
                                               or not isinstance(value, int)):
                return _err(400, "kookmomenten moeten een getal zijn")
            else:
                # cooking moments outside 1 .. eat days are bounded, not refused
                value = await ctx.hass.set_setting(entity, value)
        except ValueError:
            return _err(400, "geen weekmenu-helper")
        return web.json_response({"ok": True, "value": value})

    app.router.add_get("/", index)
    app.router.add_get("/gids", guide)
    app.router.add_get("/api/status", status)
    app.router.add_post("/api/login", login)
    app.router.add_post("/api/logout", logout)
    app.router.add_get("/api/health", health)
    app.router.add_get("/api/diagnose", get_diagnose)
    app.router.add_post("/api/diagnose/retry", retry_writes)
    app.router.add_get("/api/week", week)
    app.router.add_get("/api/dish/{day}", dish)
    app.router.add_post("/api/dish/{day}/swap", swap)
    app.router.add_post("/api/dish/{day}/back", back)
    app.router.add_post("/api/dish/{day}/persons", persons)
    app.router.add_post("/api/dish/{day}/remove", remove)
    app.router.add_post("/api/run", run)
    app.router.add_get("/api/staples", get_staples)
    app.router.add_post("/api/staples/{pid}", add_staple)
    app.router.add_get("/api/offers", get_offers)
    app.router.add_post("/api/offers/{pid}", add_offer)
    app.router.add_get("/api/savings", savings)
    app.router.add_get("/api/settings", get_settings)
    app.router.add_post("/api/settings", post_settings)
    if static_dir.is_dir():
        app.router.add_static("/static/", static_dir)
    app.router.add_get("/" + _ASSET, asset)
    return app
