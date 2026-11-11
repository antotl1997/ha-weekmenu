"""v1.0 "Besparing nagerekend": after the delivery, the real order next to what the add-on
added, and the two numbers for the app and the diagnose.

Once per menu week, from the day after its delivery (the day the add-on read when it
wrote to that order; unknown: the Wednesday after the menu week), the hourly look reads
the delivered order: the order the week was written to (`added_to` "order:<id>"), else
every delivered order with its slot in the menu week. Only reads (a GraphQL query and
GETs), under `ctx.lock` like every AH call that may refresh the login. Not while writing
is paused (AH seems changed) or without a usable login; a changed answer counts for
ApiHealth like any other read of the order.

The result goes into `/data/reconciliation.json` (schema, durable), one entry per menu
week, and is never redone: the entry is written before the reads (a crash or a failing
read leaves "niet na te rekenen", never a second try). Per week:

- "ok": `bought_share` {"bought": x, "added": y}, x of the y products the add-on added
  that were really delivered, and `realised_savings`, the deal saving (deals.savings,
  with the bonus price of the week) over what was really bought, at most what was
  added; `estimated` is the week's own estimate, for the screen;
- "niet_na_te_rekenen" with a reason: no delivered order in the menu week (a till
  receipt cannot be matched), or AH could not be read.

Neither the week logs nor totaal.json are ever written here: the cumulative total stays
the estimate the spec defines.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import date, datetime, timedelta
from pathlib import Path

from app.ahclient import AhLoginExpired, DeliveredOrder, Product
from app.deals import savings as deal_savings
from app.durable import SchemaTooNew, too_new, write_json
from app.ingredients import override_key
from app.planner import Plan
from app.staples import staple_list

log = logging.getLogger("weekmenu.reconcile")

__all__ = ["DeliveredOrder", "Reconciliations", "compute", "due_day", "reconcile_due",
           "untouched_share", "bought_share", "metrics"]

RECON_SCHEMA = 1
OK = "ok"
UNCHECKABLE = "niet_na_te_rekenen"
BUSY = "bezig"  # the marker written before the reads
WINDOW_DAYS = 35  # weeks whose delivery is longer ago are not read any more
LAST_WEEKS = 4  # the two numbers: over the last four weeks
NO_ORDER = ("Geen bezorgde bestelling gevonden in de menuweek. Gekocht in de winkel "
            "is niet na te rekenen.")
NOT_DELIVERED = "De bestelling van deze week is niet (of nog niet) bezorgd."
UNREADABLE = "De bestelling was niet te lezen bij AH."


class Reconciliations:
    def __init__(self, path: Path):
        self.path = Path(path)

    def load(self) -> dict[str, dict]:
        """ISO menu week -> entry. A missing or broken file is no entries (logged)."""
        try:
            raw = self.path.read_bytes()
        except FileNotFoundError:
            return {}
        except OSError as e:
            log.warning("%s niet te lezen (%s)", self.path.name, type(e).__name__)
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except ValueError:
            log.warning("%s is beschadigd", self.path.name)
            return {}
        weeks = data.get("weeks") if isinstance(data, dict) else None
        if not isinstance(weeks, dict):
            return {}
        return {k: v for k, v in weeks.items() if isinstance(k, str) and isinstance(v, dict)}

    def too_new(self) -> bool:
        return too_new(self.path, RECON_SCHEMA)

    def put(self, week_iso: str, entry: dict) -> None:
        weeks = self.load()
        weeks[week_iso] = entry
        write_json(self.path, {"weeks": weeks}, RECON_SCHEMA)


# --- the sums -------------------------------------------------------------------------------


def added_products(wlog) -> dict[int, int]:
    """What the add-on put on the list or in the order this week: menu, staples, offers."""
    out: dict[int, int] = {}
    for src in (wlog.added, wlog.extras_added, wlog.offers_added):
        for pid, n in src.items():
            if n > 0:
                out[pid] = out.get(pid, 0) + n
    return out


def due_day(wlog) -> date:
    """The day after the delivery; unknown (or outside the menu week): the Wednesday
    after the menu week, when every delivery of that week is past."""
    d = wlog.delivery
    if d is not None and wlog.week <= d <= wlog.week + timedelta(days=6):
        return d + timedelta(days=1)
    return wlog.week + timedelta(days=7)


def compute(wlog, bought: dict[int, int], at: datetime) -> dict:
    """The "ok" entry: x of y added products bought, and the deal saving over them.

    Per product what was bought counts against the menu first, then the staple, then the
    loose offer, never more than the add-on added. A loose offer counts its saving only
    when all of it was bought (its deal may need every piece); an offer from before 1.0
    has no saving of its own and counts nothing.
    """
    added = added_products(wlog)
    products: dict[int, Product] = Plan.from_dict(wlog.plan).products if wlog.plan else {}
    staples = {s.product.id: s.product for s in staple_list(wlog)}
    saved = 0.0
    for pid in added:
        left = bought.get(pid, 0)
        n = min(left, wlog.added.get(pid, 0))
        if n > 0 and pid in products:
            saved += deal_savings(products[pid], n)
        left -= n
        n = min(left, wlog.extras_added.get(pid, 0))
        if n > 0 and pid in staples:
            saved += deal_savings(staples[pid], n)
        left -= n
        want = wlog.offers_added.get(pid, 0)
        if want > 0 and left >= want:
            saved += wlog.offers_saved.get(pid, 0.0)
    return {
        "status": OK,
        "bought_share": {"bought": sum(1 for pid in added if bought.get(pid, 0) > 0),
                         "added": len(added)},
        "realised_savings": round(saved, 2),
        "estimated": round(wlog.savings + wlog.extras_total, 2),
        "at": at.isoformat(timespec="minutes"),
    }


def uncheckable(reason: str, wlog, at: datetime) -> dict:
    return {"status": UNCHECKABLE, "reason": reason,
            "estimated": round(wlog.savings + wlog.extras_total, 2),
            "at": at.isoformat(timespec="minutes")}


# --- the two numbers ----------------------------------------------------------------------


def untouched_share(ledger, overrides, history, today: date) -> dict | None:
    """Of the ingredient lines of the last four menu weeks with a menu (started on or
    before today): the share without a correction. A line counts as corrected when a
    "Klopt niet" choice for its ingredient existed by the end of that week, or one was
    made in that week (history.json "correct", also when taken back later)."""
    weeks = [w for w in ledger.weeks() if w.plan and not w.too_new and w.week <= today]
    weeks = weeks[-LAST_WEEKS:]
    if not weeks:
        return None
    chosen = overrides.load()
    corrected = history.load()
    lines = untouched = 0
    for w in weeks:
        end = (w.week + timedelta(days=6)).isoformat()
        keys = {k for k, e in chosen.items() if (e.get("at") or "")[:10] <= end}
        keys |= {e.get("key") for e in corrected.get(w.week.isoformat(), [])
                 if e.get("action") == "correct" and e.get("key")}
        for d in Plan.from_dict(w.plan).dishes:
            for text, name in d.recipe.ingredients:
                lines += 1
                if override_key(name or text) not in keys:
                    untouched += 1
    if not lines:
        return None
    return {"weeks": len(weeks), "lines": lines, "untouched": untouched,
            "share": round(untouched / lines, 4)}


def bought_share(entries: dict[str, dict]) -> dict | None:
    """Of the products the add-on added in the last four weeks that could be
    recounted: the share that was really bought."""
    ok = sorted((k, v) for k, v in entries.items()
                if v.get("status") == OK and isinstance(v.get("bought_share"), dict))
    ok = ok[-LAST_WEEKS:]
    bought = sum(int(v["bought_share"].get("bought", 0)) for _, v in ok)
    added = sum(int(v["bought_share"].get("added", 0)) for _, v in ok)
    if not added:
        return None
    return {"weeks": len(ok), "bought": bought, "added": added,
            "share": round(bought / added, 4)}


def metrics(ctx, today: date) -> dict:
    """Both numbers, for api/savings and the diagnose (None: nothing to count yet)."""
    return {"untouched": untouched_share(ctx.ledger, ctx.overrides, ctx.history, today),
            "bought": bought_share(ctx.reconciliations.load())}


# --- the hourly look ---------------------------------------------------------------------


def week_to_check(ledger, entries: dict[str, dict], today: date):
    """The oldest menu week that is due and not recounted yet (or None)."""
    for w in ledger.weeks():
        if (w.too_new or not w.plan or w.week.isoformat() in entries
                or not added_products(w)):
            continue
        due = due_day(w)
        if due <= today and today - w.week <= timedelta(days=WINDOW_DAYS):
            return w
    return None


async def _bought(ctx, wlog) -> tuple[dict[int, int] | None, str | None]:
    """What was delivered for this week, or None and why not."""
    dest = wlog.added_to or ""
    if dest.startswith("order:") and dest[6:].isdigit():
        got = await ctx.client.delivered_order(int(dest[6:]))
        if got is None:
            return None, NOT_DELIVERED
        orders = [got]
    else:
        orders = await ctx.client.delivered_orders(wlog.week, wlog.week + timedelta(days=6))
        if not orders:
            return None, NO_ORDER
    bought: dict[int, int] = {}
    for o in orders:
        for pid, n in o.items.items():
            bought[pid] = bought.get(pid, 0) + n
    return bought, None


async def reconcile_due(ctx, now: datetime) -> None:
    """Recount at most one due menu week. Never raises for AH; never writes to AH, the
    week logs or the total."""
    from app.main import note_too_new, short_error  # main imports this module

    if now.time() < ctx.schedule_time:
        return
    if ctx.health.writes_paused or ctx.login_expired:
        return
    hour = now.replace(minute=0, second=0, microsecond=0)
    if getattr(ctx, "recon_checked", None) == hour:
        return  # the week logs are looked at once an hour, not every minute
    ctx.recon_checked = hour
    recon = ctx.reconciliations
    if recon.too_new():
        note_too_new(ctx, recon.path)
        return
    entries = recon.load()
    if week_to_check(ctx.ledger, entries, now.date()) is None:
        return
    if ctx.store.load() is None:
        return
    async with ctx.lock:
        entries = recon.load()
        wlog = week_to_check(ctx.ledger, entries, now.date())
        if wlog is None:
            return
        iso = wlog.week.isoformat()
        try:
            await ctx.client.ensure_fresh()  # an expired login does not use up the week
        except AhLoginExpired:
            ctx.login_expired = True
            log.warning("Nagerekende besparing: AH-login verlopen")
            return
        except Exception as e:
            log.warning("Nagerekende besparing: login niet ververst (%s)", short_error(e))
            return
        try:
            # the marker first: whatever happens next, this week is read only once
            await asyncio.to_thread(recon.put, iso, {"status": BUSY,
                                                     "at": now.isoformat(timespec="minutes")})
        except SchemaTooNew as e:
            note_too_new(ctx, e.path)
            return
        except OSError as e:
            log.warning("reconciliation.json niet geschreven (%s)", type(e).__name__)
            return
        try:
            bought, why = await _bought(ctx, wlog)
        except AhLoginExpired:
            ctx.login_expired = True
            bought, why = None, UNREADABLE
        except Exception as e:
            log.warning("Nagerekende besparing: bestelling niet te lezen (%s)", short_error(e))
            bought, why = None, UNREADABLE
        entry = (compute(wlog, bought, now) if bought is not None
                 else uncheckable(why, wlog, now))
        try:
            await asyncio.to_thread(recon.put, iso, entry)
        except Exception as e:
            log.warning("reconciliation.json niet geschreven (%s)", type(e).__name__)
            return
        if entry["status"] == OK:
            share = entry["bought_share"]
            log.info("Week %s nagerekend: %d van %d producten gekocht, echt bespaard %.2f",
                     iso, share["bought"], share["added"], entry["realised_savings"])
        else:
            log.info("Week %s niet na te rekenen", iso)
