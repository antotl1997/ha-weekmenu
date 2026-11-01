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
means it did not (drop the entry); anything else, a failing read, or an order
that is gone or has another id keeps the entry, and that product is not
written this round (never write blind).

Deliberate gap: if the user removes the product again in the seconds between
a crash and the next round, the entry looks "not applied" and is written once
more.
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

    async def current_of(entry: dict, pid: int) -> int | None:
        tgt = entry.get("target")
        if tgt not in reads:
            try:
                if tgt == "order":
                    reads[tgt] = await client.active_order()
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
            if data.id != entry.get("order_id"):
                return None
            return data.items.get(pid, 0)
        return data.get(pid, 0)

    for pid, entry in list(log.pending.items()):
        cur = await current_of(entry, pid)
        if cur is None:
            continue
        before, delta = int(entry["before"]), int(entry["delta"])
        if cur >= before + delta:
            log.added[pid] = log.added.get(pid, 0) + delta
            del log.pending[pid]
        elif cur <= before:
            del log.pending[pid]
    ledger.save(log)


async def apply(
    client, ledger: Ledger, log: WeekLog, plan: Plan, replaced: Plan | None = None
) -> WriteResult:
    await _settle(client, ledger, log)
    delta = {
        p: n - log.added.get(p, 0)
        for p, n in plan.basket.items()
        if n - log.added.get(p, 0) > 0 and p not in log.pending
    }
    order = await client.active_order()
    target: Literal["order", "list"] = "order" if order is not None else "list"
    order_id = order.id if order is not None else None
    log.plan = plan.to_dict()
    log.savings = plan.savings
    log.target = target
    ledger.save(log)

    result = WriteResult(target=target)
    try:
        for pid, qty in delta.items():

            def before_write(current: dict[int, int], pid=pid, qty=qty) -> None:
                log.pending[pid] = {
                    "before": int(current.get(pid, 0)),
                    "delta": qty,
                    "target": target,
                    "order_id": order_id,
                }
                ledger.save(log)

            if order is not None:
                await client.add_to_order(order, {pid: qty}, on_before=before_write)
            else:
                await client.add_to_shopping_list({pid: qty}, on_before=before_write)
            log.added[pid] = log.added.get(pid, 0) + qty
            log.pending.pop(pid, None)
            result.added[pid] = qty
            ledger.save(log)
    except BaseException:
        _safe_save(ledger, log)
        raise

    left = leftovers(log, plan)
    if replaced is not None:
        left = {p: n for p, n in left.items() if p in replaced.basket}
    result.leftovers = [(_title(plan, replaced, p), n) for p, n in left.items()]
    return result
