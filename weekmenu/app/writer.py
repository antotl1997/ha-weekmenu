"""Write the delta between plan and log to the AH order or shopping list.

Callers MUST serialise all calls to `apply` (one asyncio.Lock around every
write): two concurrent rounds could add
products twice.

Rules: one AH call per product, any error stops the round (progress kept,
error re-raised) and nothing is retried.

Lowering (opschonen): only when `apply` gets a replacing plan and `today` before
the menu week starts, and only on the destination everything in `log.added` went
to (`log.added_to`: the list, or that one order). Per product the add-on lowers
what it added itself and the new plan no longer needs: max(0, log.added - basket).
AH gets max(0, current - that), so what the user put there besides the add-on stays.
Staples (`extras_added`) are never lowered. A lowering has the same write-ahead
record with "kind": "reduce", a delta <= 0 and "remove" (what leaves `log.added`):
current <= before+delta means it arrived, current >= before that it did not (the
entry is dropped), anything else moves it to `log.unknown` (with the negative delta).
Never repeated blind. A product crossed off on the list is never lowered; it stays
a leftover. What a lowering finds already gone (the user took it off in the AH app:
remove - min(remove, before)) goes into `log.removed_by_user` and is never added
again that week (Ruling 43): `apply` adds only n - added - removed_by_user.

Write-ahead record: right before each PUT/PATCH the quantity AH holds
(`before`) and the amount we are about to add (`delta`) go into `log.pending`
and the log is saved. On success the entry moves to `log.added`. If the call
fails or we crash, the entry stays. At the start of the next `apply` every
pending entry is settled with one read on its own target: current >=
before+delta means AH did apply it (count it as added); current <= before
means it did not (drop the entry); an ambiguous quantity or a failing read
keeps the entry, and that product is not written this round (never write
blind). An entry whose order is gone or has another id can never be resolved:
it moves to `log.unknown` (pid -> delta), is reported and never written again
that week.

Staples (vaste boodschappen) go through `apply_extras` with the same loop and
the same pending/settle rules; their pending entry has "kind": "extra" and
settles into `log.extras_added` instead of `log.added`. Loose offers (tab
Aanbiedingen) use the same route with "kind": "offer": they settle into
`log.offers_added`, and their saving (kept in the pending entry as "savings")
into `log.offers_savings`, in the same save. Neither is ever lowered.

Which order (Ruling 42): an active order is only written to (added, lowered,
staples, offers) when its delivery day lies inside the log's menu week (Wednesday
to Tuesday). Another week's order, or one whose day cannot be read, gets nothing:
the plan is saved, `log.held` says why and the result carries `held` (the note
for the message and the app). Never a fallback to the wrong order. Only the week the
schedule works on (menu_week_start of `today`) is retried later, so only that week's
note promises it (`held_note`); any other week, and a staple or offer tap, gets the
titles to add by hand (`manual_note`, Ruling 44). With nothing to write there is no
note at all.

Writes paused (v0.7, ApiHealth): when AH seems to have changed its API
(`client.health.writes_paused`), `apply` and `apply_extras` make no AH call at all, not
even the settle reads. `apply` still saves the plan; with something to write it marks
the round as not done (`round_done` and `completion_tried` off), so the hourly look
completes it once writing is on again. The result carries `held` = PAUSED_NOTE.

Deliberate gap: the gap lasts until the next `apply`. If AH did not apply a
write, or the user removes the product again before the next round, the entry
looks "not applied" and the product is written once more. Example: a list
write crashed after AH applied it, the user deletes the item from the list
before the next round, current <= before, so it is written once more.
"""
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Literal

from app.durable import SchemaTooNew
from app.health import PAUSED_NOTE
from app.ledger import Ledger, WeekLog, leftovers
from app.planner import Plan
from app.week import lange_datum, menu_week_start


@dataclass
class WriteResult:
    target: Literal["order", "list"]
    added: dict[int, int] = field(default_factory=dict)
    removed: dict[int, int] = field(default_factory=dict)  # pid -> how many fewer at AH
    leftovers: list[tuple[str, int]] = field(default_factory=list)  # (product title, quantity)
    pending: list[tuple[str, int]] = field(default_factory=list)  # outcome still unsure
    unknown: list[tuple[str, int]] = field(default_factory=list)  # status unknown, check in the AH app
    held: str | None = None  # nothing written: no order or list for this week yet (the note)


EXTRA = "extra"  # pending "kind" of a staple write
OFFER = "offer"  # pending "kind" of a loose offer write
REDUCE = "reduce"  # pending "kind" of a lowering

log_ = logging.getLogger("weekmenu.writer")


def _log_target(what: str, order, count: int) -> None:
    """Where this write goes and why; never a token or a body."""
    why = (f"actieve bestelling {order.id}" if order is not None
           else "geen actieve bestelling -> lijst")
    log_.info("%s: %d product(en) naar %s", what, count, why)


def manual_note(titles) -> str:
    """For a week (or a tap) nothing will retry: what to add in the AH app yourself."""
    return ("Deze producten konden niet in je bestelling. Voeg ze zelf toe in de "
            "AH-app: " + ", ".join(titles))


def cut_note(titles) -> str:
    """Lowerings that could not go out: never retried, the user decides."""
    return ("Deze producten konden niet uit je bestelling. Haal ze zelf weg in de "
            "AH-app als dat moet: " + ", ".join(titles))


def missing(log: WeekLog, plan: Plan) -> dict[int, int]:
    """What `plan` needs that the add-on has not put out there (nor the user removed)."""
    out = {}
    for p, n in plan.basket.items():
        if p in log.pending or p in log.unknown:
            continue
        left = n - log.added.get(p, 0) - log.removed_by_user.get(p, 0)
        if left > 0:
            out[p] = left
    return out


def retried(week: date, today: date | None) -> bool:
    """The schedule retries a held week only while it is the week it works on."""
    if today is None:
        return True
    return week == menu_week_start(datetime.combine(today, time()))


def held_note(week: date) -> str:
    """For the message and the app when a write waits for this week's order or list."""
    return ("Het menu staat klaar; de producten worden toegevoegd zodra er een "
            f"bestelling of lijst voor {lange_datum(week)} is")


async def _destination(client, week: date):
    """The active order (or None: the list) and, when it may not be written, why."""
    order = await client.active_order()
    if order is None:
        return None, None
    day = await client.order_delivery_date(order)
    if day is None:
        return order, f"bezorgdatum van bestelling {order.id} onbekend"
    if not week <= day <= week + timedelta(days=6):
        return order, (f"bestelling {order.id} wordt {day.isoformat()} bezorgd, "
                       f"buiten de menuweek van {week.isoformat()}")
    return order, None


def _title(plan: Plan, replaced: Plan | None, pid: int) -> str:
    for src in (plan, replaced):
        if src is not None and pid in src.products:
            return src.products[pid].title
    return str(pid)


def _refuse_too_new(ledger: Ledger, log: WeekLog) -> None:
    """Before any AH call: a log this code may not write means no write at AH either
    (the write-ahead save would refuse anyway; this stops the reads too)."""
    if log.too_new:
        raise SchemaTooNew(ledger.root / f"{log.week.isoformat()}.json")


def writes_paused(client) -> bool:
    """ApiHealth says AH seems to have changed: nothing may go to AH."""
    health = getattr(client, "health", None)
    return bool(health is not None and health.writes_paused)


def _safe_save(ledger: Ledger, log: WeekLog) -> None:
    try:
        ledger.save(log)
    except Exception:
        pass  # the original AH error is more important


async def _settle(client, ledger: Ledger, log: WeekLog) -> None:
    if not log.pending:
        return
    reads: dict[str, object] = {}  # one read per target; None = unusable
    gone = object()

    async def current_of(entry: dict, pid: int):
        tgt = entry.get("target")
        if tgt not in reads:
            try:
                if tgt == "order":
                    order = await client.active_order()
                    reads[tgt] = order if order is not None else False
                elif tgt == "list":
                    reads[tgt] = await client.shopping_list()
                else:
                    reads[tgt] = None
            except Exception:
                reads[tgt] = None
        data = reads[tgt]
        if data is None:
            return None
        if tgt == "order":
            if data is False or data.id != entry.get("order_id"):
                return gone
            return data.items.get(pid, 0)
        return data.get(pid, 0)

    for pid, entry in list(log.pending.items()):
        cur = await current_of(entry, pid)
        if cur is None:
            continue
        if cur is gone:
            log.unknown[pid] = int(entry["delta"])
            del log.pending[pid]
            continue
        before, delta = int(entry["before"]), int(entry["delta"])
        if entry.get("kind") == REDUCE:
            if cur <= before + delta:  # arrived
                remove = int(entry.get("remove", -delta))
                _take(log.added, pid, remove)
                _user_removed(log, pid, remove, before)
            elif cur < before:  # partly lower: nobody can tell what happened
                log.unknown[pid] = delta
            del log.pending[pid]
            continue
        if cur >= before + delta:
            kind = entry.get("kind")
            into = (log.extras_added if kind == EXTRA
                    else log.offers_added if kind == OFFER else log.added)
            into[pid] = into.get(pid, 0) + delta
            if kind == OFFER:
                log.offers_savings = round(log.offers_savings + float(entry.get("savings") or 0), 2)
            del log.pending[pid]
        elif cur <= before:
            del log.pending[pid]
    ledger.save(log)


def _take(into: dict[int, int], pid: int, n: int) -> None:
    left = into.get(pid, 0) - n
    if left > 0:
        into[pid] = left
    else:
        into.pop(pid, None)


def _user_removed(log: WeekLog, pid: int, remove: int, before: int) -> None:
    """The part of a lowering AH no longer had: the user took it off (Ruling 43)."""
    gone = remove - min(remove, max(0, before))
    if gone > 0:
        log.removed_by_user[pid] = log.removed_by_user.get(pid, 0) + gone


def _dest(target: str, order) -> str:
    return f"order:{order.id}" if order is not None else target


async def _reduce_each(
    client, ledger: Ledger, log: WeekLog, order, cuts: dict[int, int], result: WriteResult,
) -> None:
    """One AH call per product, write-ahead `pending` (kind "reduce") before each.

    Any error stops the loop: progress is saved and the error re-raised, never retried.
    """
    target = result.target
    order_id = order.id if order is not None else None
    try:
        for pid, r in cuts.items():
            change: list[int] = []

            def before_write(current: dict[int, int], pid=pid, r=r, change=change) -> None:
                before = int(current.get(pid, 0))
                delta = max(0, before - r) - before
                change.append(delta)
                change.append(before)
                log.pending[pid] = {
                    "before": before,
                    "delta": delta,
                    "remove": r,
                    "target": target,
                    "order_id": order_id,
                    "kind": REDUCE,
                }
                ledger.save(log)

            if order is not None:
                await client.reduce_in_order(order, {pid: r}, on_before=before_write)
            else:
                skipped = await client.reduce_on_shopping_list({pid: r}, on_before=before_write)
                if pid in (skipped or set()):  # crossed off on the list: left alone, a leftover
                    log.pending.pop(pid, None)
                    ledger.save(log)
                    continue
            _take(log.added, pid, r)
            if change:
                _user_removed(log, pid, r, change[1])
            log.pending.pop(pid, None)
            if change and change[0] < 0:
                result.removed[pid] = -change[0]
            ledger.save(log)
    except BaseException:
        _safe_save(ledger, log)
        raise


async def _write_each(
    client, ledger: Ledger, log: WeekLog, order, delta: dict[int, int],
    into: dict[int, int], result: WriteResult, kind: str | None = None,
    gain: dict[int, float] | None = None,
) -> None:
    """One AH call per product, write-ahead `pending` before each, `into` after each.

    Any error stops the loop: progress is saved and the error re-raised, never retried.
    """
    target = result.target
    order_id = order.id if order is not None else None
    try:
        for pid, qty in delta.items():

            def before_write(current: dict[int, int], pid=pid, qty=qty) -> None:
                entry = {
                    "before": int(current.get(pid, 0)),
                    "delta": qty,
                    "target": target,
                    "order_id": order_id,
                }
                if kind is not None:
                    entry["kind"] = kind
                if kind == OFFER:
                    entry["savings"] = float((gain or {}).get(pid, 0.0))
                log.pending[pid] = entry
                ledger.save(log)

            if order is not None:
                await client.add_to_order(order, {pid: qty}, on_before=before_write)
            else:
                await client.add_to_shopping_list({pid: qty}, on_before=before_write)
            into[pid] = into.get(pid, 0) + qty
            if kind == OFFER:
                log.offers_savings = round(log.offers_savings + (gain or {}).get(pid, 0.0), 2)
            log.pending.pop(pid, None)
            result.added[pid] = qty
            ledger.save(log)
    except BaseException:
        _safe_save(ledger, log)
        raise


async def apply(
    client, ledger: Ledger, log: WeekLog, plan: Plan, replaced: Plan | None = None,
    today: date | None = None,
) -> WriteResult:
    """Write what `plan` needs and the log has not added yet.

    With a replacing plan (`replaced`) and `today` before the menu week, also lower
    what the add-on added for the old plan and `plan` no longer needs (see the
    module docstring for when that is safe).
    A log from a newer add-on is never written, so nothing goes to AH (SchemaTooNew).
    """
    _refuse_too_new(ledger, log)
    if writes_paused(client):
        return _paused_apply(ledger, log, plan, replaced, today)
    await _settle(client, ledger, log)
    # what the add-on put out there, plus what the user took off again (never re-added)
    delta = missing(log, plan)
    order, why = await _destination(client, log.week)
    target: Literal["order", "list"] = "order" if order is not None else "list"
    dest = _dest(target, order)
    cuts: dict[int, int] = {}
    if (replaced is not None and today is not None and today < log.week
            and log.added_to == dest):
        cuts = {
            p: n - plan.basket.get(p, 0)
            for p, n in log.added.items()
            if n - plan.basket.get(p, 0) > 0 and p not in log.pending and p not in log.unknown
        }
    if why is not None and (delta or cuts):  # another week's order: write nothing at all
        log.plan = plan.to_dict()
        log.savings = plan.savings
        log.target = target  # where it would have gone: never "list" by mistake
        log.held = why
        if log.held_notified and sorted(delta) != log.held_notified_items:
            log.held_notified = False  # other products missing now: push again once
        ledger.save(log)
        log_.info("Menu: niets geschreven, %s", why)
        if delta and retried(log.week, today):
            note = held_note(log.week)
        elif delta:
            note = manual_note(_title(plan, replaced, p) for p in delta)
        else:  # only lowerings: no round ever retries those
            note = cut_note(_title(plan, replaced, p) for p in cuts)
        result = WriteResult(target=target, held=note)
        _report(result, log, plan, replaced)
        return result
    log.held = None
    log.plan = plan.to_dict()
    log.savings = plan.savings
    if why is None:  # nothing to write into another week's order: keep the old target
        log.target = target
    elif log.target is not None:
        target = log.target
    if delta:
        if not log.added:
            log.added_to = dest
        elif log.added_to != dest:
            log.added_to = "mixed"  # never lowered again: the adds are on two places
    ledger.save(log)
    _log_target("Menu", order, len(delta))

    result = WriteResult(target=target)
    await _write_each(client, ledger, log, order, delta, log.added, result)
    if cuts:
        _log_target("Opschonen", order, len(cuts))
        await _reduce_each(client, ledger, log, order, cuts, result)

    _report(result, log, plan, replaced)
    return result


def _paused_apply(ledger: Ledger, log: WeekLog, plan: Plan, replaced: Plan | None,
                  today: date | None) -> WriteResult:
    """Writes paused: keep the plan, write nothing, and leave the round open when
    something is still to be written (or lowered), so the hourly look completes it."""
    delta = missing(log, plan)
    may_cut = (replaced is not None and today is not None and today < log.week
               and any(n - plan.basket.get(p, 0) > 0 for p, n in log.added.items()))
    log.plan = plan.to_dict()
    log.savings = plan.savings
    if delta or may_cut:
        log.round_done = log.completion_tried = False
    ledger.save(log)
    target = log.target or "list"
    result = WriteResult(target=target, held=PAUSED_NOTE if (delta or may_cut) else None)
    if result.held:
        log_.warning("Menu: niets geschreven, schrijven staat stil (AH lijkt veranderd)")
    _report(result, log, plan, replaced)
    return result


def _report(result: WriteResult, log: WeekLog, plan: Plan, replaced: Plan | None) -> None:
    left = leftovers(log, plan)
    if replaced is not None:
        left = {p: n for p, n in left.items() if p in replaced.basket}
    result.leftovers = [(_title(plan, replaced, p), n) for p, n in left.items()]
    result.pending = [(_title(plan, replaced, p), int(e["delta"])) for p, e in log.pending.items()]
    result.unknown = [(_title(plan, replaced, p), n) for p, n in log.unknown.items()]


async def apply_extras(
    client, ledger: Ledger, log: WeekLog, adds: dict[int, int],
    titles: dict[int, str] | None = None, kind: str = EXTRA,
    savings: dict[int, float] | None = None,
) -> WriteResult:
    """Add staples (vaste boodschappen) or, with kind="offer", loose offers, with the
    same guarantees as `apply`. `savings`: the saving per offer, kept with the write.

    Same lock rule as `apply`. A product goes in once per week: whatever is in
    `log.extras_added` (`log.offers_added` for an offer) is never written again, an unsure (`pending`) or `unknown`
    product is skipped. Settling first means a write whose outcome was unsure is
    read back before anything new is written.
    """
    _refuse_too_new(ledger, log)
    titles = titles or {}
    into = log.offers_added if kind == OFFER else log.extras_added
    if writes_paused(client):
        todo = [p for p, n in adds.items() if n > 0 and p not in into]
        result = WriteResult(target=log.target or "list", held=PAUSED_NOTE if todo else None)
        if todo:
            log_.warning("%s: niets geschreven, schrijven staat stil (AH lijkt veranderd)",
                         "Losse aanbieding" if kind == OFFER else "Vaste boodschappen")
        return result
    await _settle(client, ledger, log)
    delta = {
        p: n for p, n in adds.items()
        if n > 0 and p not in into and p not in log.pending and p not in log.unknown
    }
    order, why = await _destination(client, log.week)
    result = WriteResult(target="order" if order is not None else "list")
    if why is not None and delta:  # another week's order: nothing written, the caller says so
        log_.info("%s: niets geschreven, %s",
                  "Losse aanbieding" if kind == OFFER else "Vaste boodschappen", why)
        # a tap is never retried: say what to add by hand
        result.held = manual_note(titles.get(p, str(p)) for p in delta)
        return result
    if log.target is None:
        log.target = result.target
    _log_target("Losse aanbieding" if kind == OFFER else "Vaste boodschappen", order, len(delta))
    await _write_each(client, ledger, log, order, delta, into, result, kind=kind, gain=savings)
    result.pending = [(titles.get(p, str(p)), int(log.pending[p]["delta"]))
                      for p in adds if p in log.pending]
    result.unknown = [(titles.get(p, str(p)), log.unknown[p]) for p in adds if p in log.unknown]
    return result
