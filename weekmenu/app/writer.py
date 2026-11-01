"""Write the delta between plan and log to the AH order or shopping list.

Callers MUST serialise all calls to `apply` (one asyncio.Lock around every
write): AhClient has no refresh lock and two concurrent rounds could add
products twice.

Rules: one AH call per product, the log is saved after every success, any
error stops the round (progress kept, error re-raised) and nothing is retried.
Nothing is ever removed or lowered.
"""
from dataclasses import dataclass, field
from typing import Literal

from app.ledger import Ledger, WeekLog
from app.planner import Plan


@dataclass
class WriteResult:
    target: Literal["order", "list"]
    added: dict[int, int] = field(default_factory=dict)
    leftovers: list[str] = field(default_factory=list)


def _title(plan: Plan, replaced: Plan | None, pid: int) -> str:
    for src in (replaced, plan):
        if src is not None and pid in src.products:
            return src.products[pid].title
    return str(pid)


async def apply(
    client, ledger: Ledger, log: WeekLog, plan: Plan, replaced: Plan | None = None
) -> WriteResult:
    delta = {
        p: n - log.added.get(p, 0)
        for p, n in plan.basket.items()
        if n - log.added.get(p, 0) > 0
    }
    order = await client.active_order()
    target: Literal["order", "list"] = "order" if order is not None else "list"
    log.plan = plan.to_dict()
    log.savings = plan.savings
    log.target = target
    ledger.save(log)

    result = WriteResult(target=target)
    try:
        for pid, qty in delta.items():
            if order is not None:
                await client.add_to_order(order, {pid: qty})
            else:
                await client.add_to_shopping_list({pid: qty})
            log.added[pid] = log.added.get(pid, 0) + qty
            result.added[pid] = qty
            ledger.save(log)
    except BaseException:
        ledger.save(log)
        raise

    if replaced is not None:
        result.leftovers = [
            _title(plan, replaced, p)
            for p, n in replaced.basket.items()
            if n - plan.basket.get(p, 0) > 0
        ]
    return result
