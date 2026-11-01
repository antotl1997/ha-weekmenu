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
from app.writer import apply

W = date(2026, 11, 4)
BASE = "https://api.ah.nl"


def prod(id, title=None):
    return Product(id, title or f"Product {id}", None, 5.0, 3.0, True, None, True, None)


def plan(basket):
    return Plan([], basket, {p: prod(p) for p in basket}, 2.5, [], 0)


class FakeAh:
    def __init__(self, order=None, fail_on_call=None, exc=None):
        self.order = order
        self.calls = []  # ("order"|"list", adds)
        self.fail_on_call = fail_on_call
        self.exc = exc or AhApiError("boom", 500)

    async def active_order(self):
        return self.order

    def _maybe_fail(self):
        if self.fail_on_call is not None and len(self.calls) == self.fail_on_call + 1:
            raise self.exc

    async def add_to_order(self, order, adds):
        assert len(adds) == 1
        self.calls.append(("order", dict(adds)))
        self._maybe_fail()

    async def add_to_shopping_list(self, adds):
        assert len(adds) == 1
        self.calls.append(("list", dict(adds)))
        self._maybe_fail()


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
    assert res.leftovers == ["Pasta"]
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
    await apply(ah, led, led.load(W), b)
    ah.calls.clear()
    res = await apply(ah, led, led.load(W), a)
    assert ah.calls == [] and res.added == {}
