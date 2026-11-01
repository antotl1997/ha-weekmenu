"""Write the delta between plan and log to the AH order or shopping list.

Callers MUST serialise all calls to `apply` (one asyncio.Lock around every
write): AhClient has no refresh lock and two concurrent rounds could add
products twice.

Rules: one AH call per product, any error stops the round (progress kept,
error re-raised) and nothing is retried. Nothing is ever removed or lowered.

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
settles into `log.extras_added` instead of `log.added`.

Deliberate gap: the gap lasts until the next `apply`. If AH did not apply a
write, or the user removes the product again before the next round, the entry
looks "not applied" and the product is written once more. Example: a list
write crashed after AH applied it, the user deletes the item from the list
before the next round, current <= before, so it is written once more.
"""
from dataclasses import dataclass, field
from typing import Literal

from app.ledger import Ledger, WeekLog, leftovers
from app.planner import Plan


@dataclass
class WriteResult:
    target: Literal["order", "list"]
    added: dict[int, int] = field(default_factory=dict)
    leftovers: list[tuple[str, int]] = field(default_factory=list)  # (product title, quantity)
    pending: list[tuple[str, int]] = field(default_factory=list)  # outcome still unsure
    unknown: list[tuple[str, int]] = field(default_factory=list)  # status unknown, check in the AH app


EXTRA = "extra"  # pending "kind" of a staple write


def _title(plan: Plan, replaced: Plan | None, pid: int) -> str:
    for src in (plan, replaced):
        if src is not None and pid in src.products:
            return src.products[pid].title
    return str(pid)


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
        if cur >= before + delta:
            into = log.extras_added if entry.get("kind") == EXTRA else log.added
            into[pid] = into.get(pid, 0) + delta
            del log.pending[pid]
        elif cur <= before:
            del log.pending[pid]
    ledger.save(log)


async def _write_each(
    client, ledger: Ledger, log: WeekLog, order, delta: dict[int, int],
    into: dict[int, int], result: WriteResult, kind: str | None = None,
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
                log.pending[pid] = entry
                ledger.save(log)

            if order is not None:
                await client.add_to_order(order, {pid: qty}, on_before=before_write)
            else:
                await client.add_to_shopping_list({pid: qty}, on_before=before_write)
            into[pid] = into.get(pid, 0) + qty
            log.pending.pop(pid, None)
            result.added[pid] = qty
            ledger.save(log)
    except BaseException:
        _safe_save(ledger, log)
        raise


async def apply(
    client, ledger: Ledger, log: WeekLog, plan: Plan, replaced: Plan | None = None
) -> WriteResult:
    await _settle(client, ledger, log)
    delta = {
        p: n - log.added.get(p, 0)
        for p, n in plan.basket.items()
        if n - log.added.get(p, 0) > 0 and p not in log.pending and p not in log.unknown
    }
    order = await client.active_order()
    target: Literal["order", "list"] = "order" if order is not None else "list"
    log.plan = plan.to_dict()
    log.savings = plan.savings
    log.target = target
    ledger.save(log)

    result = WriteResult(target=target)
    await _write_each(client, ledger, log, order, delta, log.added, result)

    left = leftovers(log, plan)
    if replaced is not None:
        left = {p: n for p, n in left.items() if p in replaced.basket}
    result.leftovers = [(_title(plan, replaced, p), n) for p, n in left.items()]
    result.pending = [(_title(plan, replaced, p), int(e["delta"])) for p, e in log.pending.items()]
    result.unknown = [(_title(plan, replaced, p), n) for p, n in log.unknown.items()]
    return result


async def apply_extras(
    client, ledger: Ledger, log: WeekLog, adds: dict[int, int],
    titles: dict[int, str] | None = None,
) -> WriteResult:
    """Add staples (vaste boodschappen) with the same guarantees as `apply`.

    Same lock rule as `apply`. A product goes in once per week: whatever is in
    `log.extras_added` is never written again, an unsure (`pending`) or `unknown`
    product is skipped. Settling first means a write whose outcome was unsure is
    read back before anything new is written.
    """
    titles = titles or {}
    await _settle(client, ledger, log)
    delta = {
        p: n for p, n in adds.items()
        if n > 0 and p not in log.extras_added and p not in log.pending and p not in log.unknown
    }
    order = await client.active_order()
    result = WriteResult(target="order" if order is not None else "list")
    if log.target is None:
        log.target = result.target
    await _write_each(client, ledger, log, order, delta, log.extras_added, result, kind=EXTRA)
    result.pending = [(titles.get(p, str(p)), int(log.pending[p]["delta"]))
                      for p in adds if p in log.pending]
    result.unknown = [(titles.get(p, str(p)), log.unknown[p]) for p in adds if p in log.unknown]
    return result
