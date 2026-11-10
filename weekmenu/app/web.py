"""Ingress page and JSON API on port 8099.

Ingress puts `/api/hassio_ingress/<token>/` in front of every path, so the page
must use relative paths (`api/week`, not `/api/week`). Every call that may write
to AH, or refresh the AH tokens, runs under `ctx.lock`.

Never in a response or in the log: the login code, a token, an Authorization header.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable
from datetime import date
from pathlib import Path

import httpx
from aiohttp import web

from app import main
from app.ahclient import LOGIN_URL, AhApiError, AhLoginExpired, extract_code
from app.hass import (
    HELPER_AAN,
    HELPER_AVONDEN,
    helper_persons,
    load_total,
    plan_view,
)
from app.ledger import leftovers
from app.planner import Dish, Plan, Settings
from app.staples import extras_savings, staple_list
from app.week import (
    WEEKDAGEN,
    away_dates,
    bestelling_zin,
    evenings,
    menu_week_start,
)
from app.writer import apply, apply_extras

log = logging.getLogger("weekmenu.web")

STATIC_DIR = Path(__file__).parent / "static"
_ASSET = r"{name:[A-Za-z0-9_-][A-Za-z0-9_.-]*\.(?:css|js|png|svg|ico|jpg|webp|json|webmanifest)}"
MIN_PERSONS, MAX_PERSONS = 1, 8

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
<ol>
<li>Open in een browser op de computer de ontwikkelaarstools (F12), tabblad Console.</li>
<li>Ga naar de AH-inlogpagina en log in.</li>
<li>Kopieer de regel met <code>appie://login-exit?code=…</code> uit de console en plak hem hieronder.</li>
</ol>
<p><a id="login" target="_blank" rel="noopener">AH-inlogpagina openen</a></p>
<textarea id="code" placeholder="appie://login-exit?code=…"></textarea>
<p><button id="send">Inloggen</button></p>
<p id="msg"></p>
</main>
<script>
const $ = (id) => document.getElementById(id);
async function status() {
  const s = await (await fetch("api/status")).json();
  $("status").textContent = (s.logged_in ? "Ingelogd bij AH." : "Niet ingelogd bij AH.")
    + (s.last_round ? " Laatste ronde: " + s.last_round + "." : "")
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


# --- views ------------------------------------------------------------------------------


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


def week_view(wlog, plan: Plan | None, start: date,
              away: Iterable[str] = ()) -> dict:
    away = set(away)
    out = {
        "week": start.isoformat(), "bestelling": bestelling_zin(start),
        "target": wlog.target, "days": [],
        "niet_thuis": [d.isoformat() for d in away_dates(start, away)],
        "geen_avonden": not evenings(start, 7, away),
        "savings": 0.0, "products": 0, "bonus_products": 0, "leftovers": [],
        "not_found": [], "short_by": 0, "pending": [], "unknown": [],
        "budget_exhausted": False, "period_warning": wlog.period_warning,
        "extras_savings": wlog.extras_savings, "staples_error": wlog.staples_error,
    }
    if plan is None:
        return out
    by_day = {d.date: d for d in plan.dishes}
    removed = set(plan.removed_days)
    out["niet_thuis"] = [i for i in out["niet_thuis"]
                         if date.fromisoformat(i) not in by_day
                         and date.fromisoformat(i) not in removed]
    for i in range(7):
        day = date.fromordinal(start.toordinal() + i)
        dag = WEEKDAGEN[day.weekday()]
        d = by_day.get(day)
        if d is not None:
            out["days"].append({"date": day.isoformat(), "dag": dag,
                                "niet_thuis": False, "persons": d.persons, "recipe": _recipe_view(d),
                                "savings": d.savings})
        elif day in removed:
            out["days"].append({"date": day.isoformat(), "dag": dag,
                                "niet_thuis": False, "geen_gerecht": True, "persons": 0,
                                "recipe": None, "savings": 0.0})
        elif dag in away:
            out["days"].append({"date": day.isoformat(), "dag": dag,
                                "niet_thuis": True, "persons": 0, "recipe": None,
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
         "added": s.product.id in wlog.extras_added}
        for s in staple_list(wlog)
    ]


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
            return _err(403, "alleen via Home Assistant")
        try:
            return await handler(request)
        except web.HTTPException:
            raise
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

    app = web.Application(middlewares=[guard_mw])

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
        view = plan_view(plan, start, target, away_dates(start, raw.away))
        await main.guard(ctx.hass.publish(view,
                                          plan.savings + extras), "sensoren")

    # --- pages ---

    async def index(request):
        page = static_dir / "index.html"
        if page.is_file():
            return web.FileResponse(page)
        return web.Response(text=FALLBACK_HTML, content_type="text/html")

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
        w = waiting_for()
        return web.json_response({
            "logged_in": logged_in, "last_round": ctx.last_round, "error": ctx.error,
            "running": ctx.busy or ctx.lock.locked(), "login_url": LOGIN_URL,
            "waiting_for_folder": None if w is None else {
                "week": w.week.isoformat(),
                "expected": w.expected.isoformat() if w.expected else None},
        })

    async def login(request):
        raw = (await body_of(request)).get("code")
        code = extract_code(raw) if isinstance(raw, str) else ""
        if not code:
            return _err(400, "Geen code gevonden. Plak de hele regel met appie://login-exit?code=…")
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
                        raw.away if raw is not None else ())
        out["enabled"] = raw.enabled if raw is not None else None  # None: HA unreadable
        w = waiting_for(start) if plan is None else None
        out["waiting_for_folder"] = None if w is None else {
            "expected": w.expected.isoformat() if w.expected else None}
        nxt = menu_week_start(now)
        out["next_week"] = nxt.isoformat() if nxt != start and ctx.ledger.load(nxt).plan else None
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
                ctx.ledger.save(wlog)
        nd = next((x for x in new.dishes if x.date == day), None)
        await publish(wlog.week, new, wlog.target or "list", wlog.extras_savings)
        # removed: how many products went off the list or out of the order (opschonen)
        done = {"removed": len(res.removed), "target": res.target}
        if nd is None:  # "Gerecht weghalen": no dish left to show
            return web.json_response({"date": day.isoformat(), **done})
        return web.json_response({**dish_view(wlog, new, nd, ctx.clock().date()), **done})

    def settings_for(plan: Plan, start: date) -> Settings:
        return Settings(
            evenings=sorted(x.date for x in plan.dishes),
            persons={x.date: x.persons for x in plan.dishes},
            pantry=list(plan.pantry or ctx.pantry),
            recent_recipe_ids=ctx.ledger.recent_recipe_ids(start),
        )

    async def swap(request):
        day = day_of(request)
        iso = day.isoformat()

        async def change(wlog, plan, d):
            bonus, _ = await main.bonus_for_week(ctx, wlog.week, ctx.clock(), fallback=True)
            exclude = set(wlog.swapped_out.get(iso, [])) | {d.recipe.id}
            new = await main.swap_dish(ctx.client, plan, day, bonus,
                                       settings_for(plan, wlog.week), exclude)
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
        if not isinstance(n, int) or isinstance(n, bool) or not MIN_PERSONS <= n <= MAX_PERSONS:
            return _err(400, f"personen moet tussen {MIN_PERSONS} en {MAX_PERSONS} liggen")

        async def change(wlog, plan, d):
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
            return _err(400, "mode moet nu_maken of nieuw_menu zijn")
        if ctx.busy or ctx.lock.locked():
            return _err(409, "Er loopt al een ronde")
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
            if pid not in wlog.extras_added:
                res = await apply_extras(ctx.client, ctx.ledger, wlog, {pid: staple.qty},
                                         {pid: staple.product.title})
                target = res.target
                wlog.extras_savings = extras_savings(wlog)
                ctx.ledger.save(wlog)
                if pid not in wlog.extras_added:  # outcome unsure, or unknown: never blind
                    return _err(409, "Onzeker of gelukt, controleer in de AH-app",
                                items=staples_view(wlog))
        if wlog.plan:
            await publish(start, Plan.from_dict(wlog.plan), wlog.target or "list",
                          wlog.extras_savings)
        # target: where this product went ("order" or "list"), for the app's message
        return web.json_response({"items": staples_view(wlog), "target": target or "list"})

    # --- savings and settings ---

    async def savings(request):
        today = ctx.clock().date()
        weeks = [w for w in ctx.ledger.weeks() if w.plan]
        month = sum(w.savings + w.extras_savings for w in weeks
                    if w.counted and (w.week.year, w.week.month) == (today.year, today.month))
        return web.json_response({
            "month": round(month, 2),
            "total": round(load_total(ctx.total_path), 2),
            "weeks": [{"week": w.week.isoformat(),
                       "savings": round(w.savings + w.extras_savings, 2),
                       "counted": w.counted} for w in weeks],
        })

    async def get_settings(request):
        raw = await ctx.hass.read_settings()
        return web.json_response({
            "enabled": raw.enabled, "avonden": raw.aantal,
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
            else:
                await ctx.hass.set_setting(entity, value)
        except ValueError:
            return _err(400, "geen weekmenu-helper")
        return web.json_response({"ok": True, "value": value})

    app.router.add_get("/", index)
    app.router.add_get("/api/status", status)
    app.router.add_post("/api/login", login)
    app.router.add_get("/api/week", week)
    app.router.add_get("/api/dish/{day}", dish)
    app.router.add_post("/api/dish/{day}/swap", swap)
    app.router.add_post("/api/dish/{day}/back", back)
    app.router.add_post("/api/dish/{day}/persons", persons)
    app.router.add_post("/api/dish/{day}/remove", remove)
    app.router.add_post("/api/run", run)
    app.router.add_get("/api/staples", get_staples)
    app.router.add_post("/api/staples/{pid}", add_staple)
    app.router.add_get("/api/savings", savings)
    app.router.add_get("/api/settings", get_settings)
    app.router.add_post("/api/settings", post_settings)
    if static_dir.is_dir():
        app.router.add_static("/static/", static_dir)
    app.router.add_get("/" + _ASSET, asset)
    return app
