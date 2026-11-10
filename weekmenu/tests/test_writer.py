import json
import time
from datetime import date

import httpx
import pytest

from app import ahclient
from app.ahclient import ActiveOrder, AhApiError, AhClient, AhOrderMode, Product
from app.ledger import Ledger, WeekLog
from app.planner import Plan
from app.tokens import Tokens, TokenStore
from app.writer import apply, apply_extras

W = date(2026, 11, 4)
BASE = "https://api.ah.nl"


def prod(id, title=None):
    return Product(id, title or f"Product {id}", None, 5.0, 3.0, True, None, True, None)


def plan(basket):
    return Plan([], basket, {p: prod(p) for p in basket}, 2.5, [], 0)


class FakeAh:
    """Keeps real quantities, so a settle read sees what was applied.

    fail_on_call=N: call N (0-based) fails. fail_after=True: AH applied it
    first (timeout after the fact); otherwise it fails before applying.
    """

    def __init__(self, order=None, fail_on_call=None, exc=None, fail_after=False, listed=None):
        self.order = order
        self.listed = dict(listed or {})
        self.calls = []  # ("order"|"list", adds)
        self.fail_on_call = fail_on_call
        self.fail_after = fail_after
        self.exc = exc or AhApiError("boom", 500)

    async def active_order(self):
        if self.order is None:
            return None
        return ActiveOrder(self.order.id, dict(self.order.items))

    async def shopping_list(self):
        return dict(self.listed)

    def _write(self, kind, store, adds, on_before):
        assert len(adds) == 1
        (pid, qty), = adds.items()
        if on_before:
            on_before({pid: store.get(pid, 0)})
        failing = self.fail_on_call is not None and len(self.calls) == self.fail_on_call
        self.calls.append((kind, dict(adds)))
        if failing and not self.fail_after:
            raise self.exc
        store[pid] = store.get(pid, 0) + qty
        if failing:
            raise self.exc

    async def add_to_order(self, order, adds, on_before=None):
        self._write("order", self.order.items, adds, on_before)

    def _reduce(self, kind, store, cuts, on_before):
        """Like AH: max(0, current - r); recorded in `calls` as a negative quantity."""
        assert len(cuts) == 1
        (pid, r), = cuts.items()
        assert r > 0
        if on_before:
            on_before({pid: store.get(pid, 0)})
        failing = self.fail_on_call is not None and len(self.calls) == self.fail_on_call
        self.calls.append((kind, {pid: -r}))
        if failing and not self.fail_after:
            raise self.exc
        left = max(0, store.get(pid, 0) - r)
        if left:
            store[pid] = left
        else:
            store.pop(pid, None)
        if failing:
            raise self.exc

    async def reduce_in_order(self, order, cuts, on_before=None):
        assert order.id == self.order.id
        self._reduce("order", self.order.items, cuts, on_before)

    async def reduce_on_shopping_list(self, cuts, on_before=None):
        self._reduce("list", self.listed, cuts, on_before)

    async def add_to_shopping_list(self, adds, on_before=None):
        failing = self.fail_on_call is not None and len(self.calls) == self.fail_on_call
        if failing and isinstance(self.exc, AhOrderMode):
            self.calls.append(("list", dict(adds)))
            raise self.exc  # the list read itself fails: before on_before
        self._write("list", self.listed, adds, on_before)


@pytest.fixture
def led(tmp_path):
    return Ledger(tmp_path / "weeks")


async def test_apply_uses_order_when_active(led):
    ah = FakeAh(order=ActiveOrder(1, {}))
    log = WeekLog(W)
    res = await apply(ah, led, log, plan({1: 2, 2: 1}))
    assert res.target == "order" and res.added == {1: 2, 2: 1}
    assert ah.calls == [("order", {1: 2}), ("order", {2: 1})]
    assert log.target == "order" and led.load(W).added == {1: 2, 2: 1}


async def test_apply_uses_list_without_order(led):
    ah = FakeAh()
    log = WeekLog(W, savings=99.0)
    res = await apply(ah, led, log, plan({1: 2}))
    assert res.target == "list" and ah.calls == [("list", {1: 2})]
    saved = led.load(W)
    assert saved.target == "list" and saved.savings == 2.5  # replaced, not added
    assert saved.plan["basket"] == {"1": 2}


async def test_second_apply_adds_nothing(led):
    ah = FakeAh()
    await apply(ah, led, WeekLog(W), plan({1: 2, 2: 1}))
    ah.calls.clear()
    res = await apply(ah, led, led.load(W), plan({1: 2, 2: 1}))
    assert ah.calls == [] and res.added == {}


async def test_removed_by_user_not_readded(led):
    ah = FakeAh()
    log = WeekLog(W, added={1: 2})  # the log says 2 added; the AH list has 0
    res = await apply(ah, led, log, plan({1: 2}))
    assert ah.calls == [] and res.added == {}


async def test_failure_midway_keeps_progress_and_resumes(led):
    ah = FakeAh(fail_on_call=2)  # the 3rd call fails
    p = plan({1: 1, 2: 2, 3: 3, 4: 1})
    with pytest.raises(AhApiError):
        await apply(ah, led, WeekLog(W), p)
    assert len(ah.calls) == 3  # nothing retried, the 4th never tried
    assert led.load(W).added == {1: 1, 2: 2}
    ah2 = FakeAh()
    res = await apply(ah2, led, led.load(W), p)
    assert ah2.calls == [("list", {3: 3}), ("list", {4: 1})]
    assert res.added == {3: 3, 4: 1}
    assert led.load(W).added == {1: 1, 2: 2, 3: 3, 4: 1}


async def test_order_mode_midway_stops_and_reraises(led):
    ah = FakeAh(fail_on_call=1, exc=AhOrderMode("locked", 400))
    with pytest.raises(AhOrderMode):
        await apply(ah, led, WeekLog(W), plan({1: 1, 2: 1, 3: 1}))
    assert len(ah.calls) == 2 and all(c[0] == "list" for c in ah.calls)
    assert led.load(W).added == {1: 1}


async def test_new_menu_adds_only_difference_and_reports_leftovers(led):
    ah = FakeAh()
    old = plan({1: 2, 2: 1})
    old.products[2] = prod(2, "Pasta")
    await apply(ah, led, WeekLog(W), old)
    ah.calls.clear()
    new = plan({1: 3, 3: 1})
    res = await apply(ah, led, led.load(W), new, replaced=old)
    assert ah.calls == [("list", {1: 1}), ("list", {3: 1})]
    assert res.leftovers == [("Pasta", 1)]
    assert led.load(W).added == {1: 3, 2: 1, 3: 1}


async def test_never_lowers_quantity(respx_mock, led, tmp_path, monkeypatch):
    monkeypatch.setattr(ahclient, "CALL_DELAY", 0)
    respx_mock.get(BASE + "/mobile-services/order/v1/summaries/active").respond(
        json={"id": 7, "orderedProducts": [{"quantity": 5, "product": {"webshopId": 1}}]}
    )
    put = respx_mock.put(BASE + "/mobile-services/order/v1/items").respond(200)
    store = TokenStore(tmp_path / "t.json")
    store.save(Tokens("a", "r", time.time() + 3600))
    http = httpx.AsyncClient()
    await apply(AhClient(store, http), led, WeekLog(W), plan({1: 2}))
    items = json.loads(put.calls[0].request.content)["items"]
    assert [(i["productId"], i["quantity"]) for i in items] == [(1, 7)]  # 5 + 2
    await http.aclose()


async def test_swap_back_adds_nothing_already_bought(led):
    ah = FakeAh()
    a, b = plan({1: 2, 2: 1}), plan({3: 1})
    await apply(ah, led, WeekLog(W), a)
    ah.calls.clear()
    await apply(ah, led, led.load(W), b)
    assert ah.calls == [("list", {3: 1})]
    ah.calls.clear()
    res = await apply(ah, led, led.load(W), a)
    assert ah.calls == [] and res.added == {}


class FlakyLedger(Ledger):
    """Save fails (always) once armed, like a crash right after a successful AH call."""

    armed = False

    def save(self, log):
        if self.armed:
            raise OSError("disk")
        super().save(log)


async def test_ah_applied_but_call_raised_is_not_doubled(led):
    ah = FakeAh(fail_on_call=0, fail_after=True)
    p = plan({1: 2})
    with pytest.raises(AhApiError):
        await apply(ah, led, WeekLog(W), p)
    assert led.load(W).pending == {
        1: {"before": 0, "delta": 2, "target": "list", "order_id": None}}
    ah.fail_on_call = None
    res = await apply(ah, led, led.load(W), p)
    assert len(ah.calls) == 1 and res.added == {}
    saved = led.load(W)
    assert saved.added == {1: 2} and saved.pending == {}
    assert ah.listed == {1: 2}


async def test_ah_applied_but_call_raised_on_order_is_not_doubled(led):
    ah = FakeAh(order=ActiveOrder(5, {1: 1}), fail_on_call=0, fail_after=True)
    p = plan({1: 2})
    with pytest.raises(AhApiError):
        await apply(ah, led, WeekLog(W), p)
    ah.fail_on_call = None
    await apply(ah, led, led.load(W), p)
    assert len(ah.calls) == 1 and ah.order.items == {1: 3}
    assert led.load(W).added == {1: 2}


async def test_ah_failed_before_applying_is_written_once_next_round(led):
    ah = FakeAh(fail_on_call=0)
    p = plan({1: 2})
    with pytest.raises(AhApiError):
        await apply(ah, led, WeekLog(W), p)
    assert ah.listed == {} and 1 in led.load(W).pending
    ah.fail_on_call = None
    await apply(ah, led, led.load(W), p)
    assert len(ah.calls) == 2 and ah.listed == {1: 2}
    saved = led.load(W)
    assert saved.added == {1: 2} and saved.pending == {}


async def test_ledger_save_fails_after_successful_call_not_doubled(tmp_path):
    fl = FlakyLedger(tmp_path / "weeks")
    ah = FakeAh()
    p = plan({1: 2})
    orig = ah._write

    def arm_after_write(*a, **k):
        orig(*a, **k)
        fl.armed = True

    ah._write = arm_after_write
    with pytest.raises(OSError):
        await apply(ah, fl, WeekLog(W), p)
    fl.armed = False
    assert 1 in fl.load(W).pending  # the write-ahead record survived
    res = await apply(ah, fl, fl.load(W), p)
    assert len(ah.calls) == 1 and res.added == {}
    assert fl.load(W).added == {1: 2} and fl.load(W).pending == {}


async def test_pending_with_order_gone_moves_to_unknown_and_is_never_written(led):
    log = WeekLog(W, pending={1: {"before": 0, "delta": 2, "target": "order", "order_id": 5}})
    ah = FakeAh()  # no active order any more
    res = await apply(ah, led, log, plan({1: 2, 2: 1}))
    assert ah.calls == [("list", {2: 1})] and res.added == {2: 1}
    saved = led.load(W)
    assert saved.pending == {} and saved.unknown == {1: 2}
    assert res.unknown == [("Product 1", 2)] and res.pending == []
    ah.calls.clear()
    res2 = await apply(ah, led, led.load(W), plan({1: 2, 2: 1}))
    assert ah.calls == [] and res2.unknown == [("Product 1", 2)]  # cumulative for the week


async def test_pending_with_other_order_id_moves_to_unknown(led):
    log = WeekLog(W, pending={1: {"before": 0, "delta": 2, "target": "order", "order_id": 5}})
    ah = FakeAh(order=ActiveOrder(6, {}))
    res = await apply(ah, led, log, plan({1: 2}))
    assert ah.calls == [] and led.load(W).unknown == {1: 2} and res.unknown == [("Product 1", 2)]


async def test_pending_ambiguous_quantity_is_kept_and_skipped(led):
    log = WeekLog(W, pending={1: {"before": 1, "delta": 3, "target": "list", "order_id": None}})
    ah = FakeAh(listed={1: 2})  # between before and before+delta
    res = await apply(ah, led, log, plan({1: 3}))
    assert ah.calls == [] and 1 in led.load(W).pending
    assert res.pending == [("Product 1", 3)] and res.unknown == []


async def test_pending_read_failure_keeps_entry(led):
    class Broken(FakeAh):
        async def shopping_list(self):
            raise AhApiError("down", 500)

    log = WeekLog(W, pending={1: {"before": 0, "delta": 2, "target": "list", "order_id": None}})
    ah = Broken()
    await apply(ah, led, log, plan({1: 2}))
    assert ah.calls == [] and 1 in led.load(W).pending


async def test_original_error_survives_failing_save(tmp_path):
    fl = FlakyLedger(tmp_path / "weeks")
    ah = FakeAh(fail_on_call=0)
    orig = ah._write

    def arm(kind, store, adds, on_before):
        def hook(cur):
            on_before(cur)
            fl.armed = True  # the write-ahead save worked; every later save fails

        orig(kind, store, adds, hook)

    ah._write = arm
    with pytest.raises(AhApiError):
        await apply(ah, fl, WeekLog(W), plan({1: 1}))


async def test_leftovers_come_from_log_with_quantities_and_limited_to_replaced(led):
    ah = FakeAh()
    old = plan({1: 4, 2: 1})
    await apply(ah, led, WeekLog(W), old)
    log = led.load(W)
    log.added[9] = 2  # an older addition that is not part of the replaced menu
    res = await apply(ah, led, log, plan({1: 2}), replaced=old)
    assert res.leftovers == [("Product 1", 2), ("Product 2", 1)]


# --- extras (vaste boodschappen): same write-ahead and settle rules --------------------


async def test_apply_extras_writes_once_into_extras_added(led):
    ah = FakeAh(order=ActiveOrder(3, {9: 1}))
    log = WeekLog(W)
    res = await apply_extras(ah, led, log, {9: 2})
    assert res.target == "order" and res.added == {9: 2}
    assert ah.order.items == {9: 3}  # 1 of Anto's own + 2: never lowered
    saved = led.load(W)
    assert saved.extras_added == {9: 2} and saved.added == {} and saved.pending == {}
    res = await apply_extras(ah, led, saved, {9: 2})
    assert res.added == {} and len(ah.calls) == 1


async def test_apply_extras_crash_after_ah_applied_is_not_doubled(led):
    ah = FakeAh(fail_on_call=0, fail_after=True)
    with pytest.raises(AhApiError):
        await apply_extras(ah, led, WeekLog(W), {9: 2})
    assert led.load(W).pending == {
        9: {"before": 0, "delta": 2, "target": "list", "order_id": None, "kind": "extra"}}
    ah.fail_on_call = None
    res = await apply_extras(ah, led, led.load(W), {9: 2})
    assert len(ah.calls) == 1 and res.added == {}
    saved = led.load(W)
    assert saved.extras_added == {9: 2} and saved.added == {} and saved.pending == {}


async def test_pending_extra_settled_by_menu_apply_goes_to_extras(led):
    ah = FakeAh(fail_on_call=0, fail_after=True)
    with pytest.raises(AhApiError):
        await apply_extras(ah, led, WeekLog(W), {9: 2})
    ah.fail_on_call = None
    await apply(ah, led, led.load(W), plan({1: 1}))
    saved = led.load(W)
    assert saved.extras_added == {9: 2} and saved.added == {1: 1}


async def test_extras_skip_unsure_and_unknown_products(led):
    ah = FakeAh()
    log = WeekLog(W, unknown={8: 1},
                  pending={9: {"before": 0, "delta": 2, "target": "list", "order_id": None,
                               "kind": "extra"}})
    ah.listed = {9: 1}  # ambiguous: neither before nor before+delta
    res = await apply_extras(ah, led, log, {8: 1, 9: 2})
    assert ah.calls == [] and res.added == {}
    assert [t for t, _ in res.pending] == ["9"] and [t for t, _ in res.unknown] == ["8"]


# --- final review fixes: the chosen target is logged ------------------------------------


async def test_apply_logs_target_and_why(led, caplog):
    caplog.set_level("INFO", logger="weekmenu.writer")
    await apply(FakeAh(order=ActiveOrder(4242, {})), led, WeekLog(W), plan({1: 1}))
    await apply(FakeAh(), led, WeekLog(date(2026, 11, 11)), plan({1: 1}))
    lines = [r.getMessage() for r in caplog.records if r.name == "weekmenu.writer"]
    assert any("actieve bestelling 4242" in m for m in lines)
    assert any("geen actieve bestelling -> lijst" in m for m in lines)
    assert all(r.levelname == "INFO" for r in caplog.records if r.name == "weekmenu.writer")


async def test_apply_extras_logs_target(led, caplog):
    caplog.set_level("INFO", logger="weekmenu.writer")
    await apply_extras(FakeAh(), led, WeekLog(W), {50: 1})
    lines = [r.getMessage() for r in caplog.records if r.name == "weekmenu.writer"]
    assert any("vaste boodschappen" in m.lower() and "geen actieve bestelling -> lijst" in m
               for m in lines)
    assert not any("Bearer" in m or "token" in m.lower() for m in lines)


# --- opschonen: lowering what the add-on added and the new plan no longer needs ------

BEFORE = date(2026, 11, 1)  # the Sunday before the menu week W


async def test_swap_removes_only_own_products_no_longer_needed(led):
    ah = FakeAh(listed={4: 1})  # 4: the user's own
    old = plan({1: 2, 2: 1, 3: 1})
    await apply(ah, led, WeekLog(W), old)
    ah.calls.clear()
    res = await apply(ah, led, led.load(W), plan({1: 2, 5: 1}), replaced=old, today=BEFORE)
    assert ah.calls == [("list", {5: 1}), ("list", {2: -1}), ("list", {3: -1})]
    assert ah.listed == {1: 2, 4: 1, 5: 1}
    assert res.added == {5: 1} and res.removed == {2: 1, 3: 1} and res.leftovers == []
    saved = led.load(W)
    assert saved.added == {1: 2, 5: 1} and saved.pending == {}


async def test_users_own_part_stays_current_3_added_1_goes_to_2(led):
    ah = FakeAh(listed={1: 2})
    old = plan({1: 1})
    await apply(ah, led, WeekLog(W), old)
    assert ah.listed == {1: 3}
    res = await apply(ah, led, led.load(W), plan({2: 1}), replaced=old, today=BEFORE)
    assert ah.listed == {1: 2, 2: 1} and res.removed == {1: 1}
    assert led.load(W).added == {2: 1}


async def test_lowers_only_the_part_no_longer_needed(led):
    ah = FakeAh(order=ActiveOrder(5, {}))
    old = plan({1: 3})
    await apply(ah, led, WeekLog(W), old)
    res = await apply(ah, led, led.load(W), plan({1: 1}), replaced=old, today=BEFORE)
    assert ah.order.items == {1: 1} and res.removed == {1: 2}
    assert led.load(W).added == {1: 1}


async def test_user_removed_it_already_nothing_lower_than_zero(led):
    ah = FakeAh()
    old = plan({1: 2})
    await apply(ah, led, WeekLog(W), old)
    ah.listed[1] = 1  # the user took one off in the AH app
    res = await apply(ah, led, led.load(W), plan({}), replaced=old, today=BEFORE)
    assert ah.listed == {} and res.removed == {1: 1} and led.load(W).added == {}


async def test_extras_are_never_lowered(led):
    ah = FakeAh()
    log = WeekLog(W)
    await apply_extras(ah, led, log, {7: 1, 1: 1})
    old = plan({1: 1})
    await apply(ah, led, led.load(W), old)
    assert ah.listed == {7: 1, 1: 2}
    ah.calls.clear()
    res = await apply(ah, led, led.load(W), plan({2: 1}), replaced=old, today=BEFORE)
    assert ah.calls == [("list", {2: 1}), ("list", {1: -1})]  # never a call on 7
    assert ah.listed == {7: 1, 1: 1, 2: 1} and res.removed == {1: 1}
    assert led.load(W).extras_added == {7: 1, 1: 1}


async def test_no_lowering_without_today_or_once_the_week_started(led):
    for today in (None, W, date(2026, 11, 6)):
        ah = FakeAh()
        old = plan({1: 1, 2: 1})
        await apply(ah, led, WeekLog(W), old)
        res = await apply(ah, led, led.load(W), plan({1: 1}), replaced=old, today=today)
        assert ah.listed == {1: 1, 2: 1} and res.removed == {}
        assert res.leftovers == [("Product 2", 1)]


async def test_no_lowering_without_a_replaced_plan(led):
    ah = FakeAh()
    await apply(ah, led, WeekLog(W), plan({1: 2}))
    res = await apply(ah, led, led.load(W), plan({1: 1}), today=BEFORE)
    assert ah.listed == {1: 2} and res.removed == {}


async def test_no_lowering_on_another_destination(led):
    # added to the list, now there is an order: the order holds other things
    ah = FakeAh()
    old = plan({1: 1})
    await apply(ah, led, WeekLog(W), old)
    ah.order = ActiveOrder(5, {1: 1})
    res = await apply(ah, led, led.load(W), plan({}), replaced=old, today=BEFORE)
    assert ah.order.items == {1: 1} and ah.listed == {1: 1} and res.removed == {}
    # added to order 5, now order 6 is the active one
    ah = FakeAh(order=ActiveOrder(5, {}))
    log = WeekLog(date(2026, 11, 11))
    await apply(ah, led, log, old)
    ah.order = ActiveOrder(6, {1: 1})
    res = await apply(ah, led, led.load(log.week), plan({}), replaced=old, today=BEFORE)
    assert ah.order.items == {1: 1} and res.removed == {}


async def test_adds_on_two_destinations_are_never_lowered(led):
    ah = FakeAh()
    old = plan({1: 1})
    await apply(ah, led, WeekLog(W), old)
    ah.order = ActiveOrder(5, {})
    both = plan({1: 1, 2: 1})
    await apply(ah, led, led.load(W), both)
    assert led.load(W).added_to == "mixed"
    res = await apply(ah, led, led.load(W), plan({}), replaced=both, today=BEFORE)
    assert ah.order.items == {2: 1} and ah.listed == {1: 1} and res.removed == {}


async def test_old_log_without_destination_is_never_lowered(led):
    ah = FakeAh(listed={1: 1})
    log = WeekLog(W, added={1: 1}, target="list")  # written by version 0.3
    res = await apply(ah, led, log, plan({}), replaced=plan({1: 1}), today=BEFORE)
    assert ah.listed == {1: 1} and res.removed == {} and res.leftovers == [("Product 1", 1)]


async def test_reduce_write_ahead_entry(led):
    ah = FakeAh(listed={1: 2})
    old = plan({1: 1})
    await apply(ah, led, WeekLog(W), old)
    ah.fail_on_call = len(ah.calls)  # the lowering fails before AH applies it
    with pytest.raises(AhApiError):
        await apply(ah, led, led.load(W), plan({}), replaced=old, today=BEFORE)
    assert led.load(W).pending == {1: {"before": 3, "delta": -1, "remove": 1, "target": "list",
                                       "order_id": None, "kind": "reduce"}}


async def test_crash_after_lowering_is_not_lowered_twice(led):
    ah = FakeAh(order=ActiveOrder(5, {1: 2}))  # 2 of the user's own
    old = plan({1: 1, 2: 1})
    await apply(ah, led, WeekLog(W), old)
    ah.fail_on_call, ah.fail_after = len(ah.calls), True  # AH lowered, then the call raised
    new = plan({2: 1})
    with pytest.raises(AhApiError):
        await apply(ah, led, led.load(W), new, replaced=old, today=BEFORE)
    assert ah.order.items == {1: 2, 2: 1} and 1 in led.load(W).pending
    ah.fail_on_call = None
    n = len(ah.calls)
    res = await apply(ah, led, led.load(W), new, replaced=old, today=BEFORE)
    assert len(ah.calls) == n and ah.order.items == {1: 2, 2: 1}  # never twice
    saved = led.load(W)
    assert saved.added == {2: 1} and saved.pending == {} and saved.unknown == {}
    assert res.leftovers == []


async def test_crash_after_lowering_completed_by_a_round_without_replaced(led):
    ah = FakeAh()
    old = plan({1: 1})
    await apply(ah, led, WeekLog(W), old)
    ah.fail_on_call, ah.fail_after = len(ah.calls), True
    with pytest.raises(AhApiError):
        await apply(ah, led, led.load(W), plan({}), replaced=old, today=BEFORE)
    ah.fail_on_call = None
    n = len(ah.calls)
    await apply(ah, led, led.load(W), plan({}))  # the automatic completion round
    assert len(ah.calls) == n and ah.listed == {} and led.load(W).added == {}


async def test_lowering_that_did_not_arrive_is_dropped_and_done_once_next_time(led):
    ah = FakeAh(listed={1: 2})
    old = plan({1: 1})
    await apply(ah, led, WeekLog(W), old)
    ah.fail_on_call = len(ah.calls)  # failed before AH applied it
    with pytest.raises(AhApiError):
        await apply(ah, led, led.load(W), plan({}), replaced=old, today=BEFORE)
    ah.fail_on_call = None
    res = await apply(ah, led, led.load(W), plan({}), replaced=old, today=BEFORE)
    assert ah.listed == {1: 2} and res.removed == {1: 1}
    assert led.load(W).added == {} and led.load(W).pending == {}


async def test_lowering_with_unclear_outcome_goes_to_unknown_and_is_left_alone(led):
    log = WeekLog(W, added={1: 3}, added_to="list", pending={1: {
        "before": 5, "delta": -3, "remove": 3, "target": "list", "order_id": None,
        "kind": "reduce"}})
    ah = FakeAh(listed={1: 4})  # between 2 and 5
    old = plan({1: 3})
    res = await apply(ah, led, log, plan({}), replaced=old, today=BEFORE)
    assert ah.calls == [] and ah.listed == {1: 4}
    saved = led.load(W)
    assert saved.unknown == {1: -3} and saved.pending == {} and saved.added == {1: 3}
    assert res.unknown == [("Product 1", -3)] and res.leftovers == [("Product 1", 3)]


async def test_lowering_whose_order_is_gone_goes_to_unknown(led):
    log = WeekLog(W, added={1: 1}, added_to="order:5", pending={1: {
        "before": 1, "delta": -1, "remove": 1, "target": "order", "order_id": 5,
        "kind": "reduce"}})
    ah = FakeAh()
    await apply(ah, led, log, plan({}), replaced=plan({1: 1}), today=BEFORE)
    assert ah.calls == [] and led.load(W).unknown == {1: -1}
