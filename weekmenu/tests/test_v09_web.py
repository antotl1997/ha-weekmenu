"""v0.9 in the web API: the check card, "Klopt niet" through the apply route,
Mijn correcties, and the swap history."""

import asyncio
import json
from datetime import date, datetime

import pytest

from app import main
from app.ahclient import ActiveOrder, Product, Recipe
from app.health import PAUSED_NOTE
from app.ingredients import SKIP
from app.ledger import WeekLog
from app.overrides import make_entry
from app.planner import Dish, Line, _finish
from app.tokens import Tokens
from app.web import make_app
from app.writer import apply
from tests.fakes import W, dish, make_ctx, make_plan

WO, DO = W, date(2026, 11, 5)
PANTRY = ["olie", "zout"]


def p(pid, title, unit=None, bonus=False):
    return Product(pid, title, unit, 3.0, 2.0 if bonus else 3.0, bonus,
                   "2e halve prijs" if bonus else None, True, None)


def menu():
    """Woensdag: rode ui, kip, komijn (not found). Donderdag: rode uien."""
    ui, kip = p(1, "AH Witte uien"), p(2, "AH Kipfilet", "300 g")
    r1 = Recipe(101, "Kip met ui", "", 30, None, None, ["hoofdgerecht"], [], 2,
                [("1 rode ui", "rode ui"), ("300 g kipfilet", "kipfilet"),
                 ("1 tl komijn", "komijn")], [], None)
    r2 = Recipe(102, "Uiensoep", "", 30, None, None, ["hoofdgerecht"], [], 2,
                [("2 rode uien", "rode ui")], [], None)
    d1 = Dish(WO, r1, "x", False, [Line(ui, 1, "1 rode ui"), Line(kip, 1, "300 g kipfilet")],
              0.0, cook=30, not_found=["1 tl komijn"])
    d2 = Dish(DO, r2, "y", False, [Line(ui, 1, "2 rode uien")], 0.0, cook=30)
    return _finish([d1, d2], 0, PANTRY)


async def planned(tmp_path, **kw):
    ctx = make_ctx(tmp_path, **kw)
    ctx.store.save(Tokens("test-access", "test-refresh", 9e9))  # logged in
    await apply(ctx.client, ctx.ledger, ctx.ledger.load(W), menu())
    return ctx


async def test_check_card_bundles_what_needs_a_look(aiohttp_client, tmp_path):
    ctx = await planned(tmp_path)
    log = ctx.ledger.load(W)
    log.added[1] += 1  # one more than the menu needs: over from the previous menu
    log.pending = {2: {"before": 0, "delta": 1, "target": "list", "order_id": None}}
    ctx.ledger.save(log)
    client = await aiohttp_client(make_app(ctx))
    check = (await (await client.get("api/week")).json())["check"]
    assert check["ok"] is False
    assert check["counts"] == {"not_found": 1, "unsure": 1, "leftovers": 1, "held": 0,
                               "missing": 0,
                               "weigh": 0}
    assert check["not_found"] == [{"date": "2026-11-04", "dag": "woensdag",
                                   "title": "Kip met ui", "ingredient": "1 tl komijn"}]
    assert check["pending"] == ["AH Kipfilet"] and check["leftovers"][0]["qty"] == 1


async def test_hoeft_niet_from_the_check_card_makes_it_ok(aiohttp_client, tmp_path):
    ctx = await planned(tmp_path)
    client = await aiohttp_client(make_app(ctx))
    r = await client.post(f"api/dish/{WO}/correct",
                          json={"ingredient": "1 tl komijn", "kind": "skip"})
    assert r.status == 200, await r.text()
    body = await r.json()
    assert body["not_found"] == [] and any(ln.get("skipped") for ln in body["lines"])
    week = await (await client.get("api/week")).json()
    assert week["check"]["ok"] is True, week["check"]
    assert week["not_found"] == []
    assert ctx.overrides.load()["komijn"]["kind"] == SKIP


async def test_ander_product_goes_through_apply_on_every_dish(aiohttp_client, tmp_path):
    ctx = await planned(tmp_path)
    ah = ctx.client
    ah.listed[4] = 1  # the user's own: never touched
    assert ah.held() == {1: 2, 2: 1, 4: 1}
    ah.search_results["rode ui"] = [p(7, "AH Rode uien", "3 stuks"),
                                    Product(8, "Uitverkocht", None, 1, 1, False, None, False,
                                            None)]
    client = await aiohttp_client(make_app(ctx))
    r = await client.get("api/products", params={"q": "rode ui"})
    assert r.status == 200
    items = (await r.json())["items"]
    assert [x["id"] for x in items] == [7]  # only orderable products

    r = await client.post(f"api/dish/{WO}/correct",
                          json={"ingredient": "1 rode ui", "kind": "product", "product_id": 7})
    assert r.status == 200, await r.text()
    body = await r.json()
    # 1 + 2 red onions from one pack of 3; both packs of white onions off the list
    assert ah.held() == {2: 1, 4: 1, 7: 1}
    assert body["removed"] == 1 and body["target"] == "list"
    line = next(x for x in body["lines"] if x["ingredient"] == "1 rode ui")
    assert line["product_id"] == 7 and line["override"] == "product"
    log = ctx.ledger.load(W)
    assert log.added == {2: 1, 7: 1} and log.pending == {}
    do = await (await client.get(f"api/dish/{DO}")).json()
    assert do["lines"][0]["product_id"] == 7
    # the next menus use it too, without a search
    s = main.settings_from_plan(ctx, menu(), W)
    assert s.overrides["rode ui"]["product"]["id"] == 7


async def test_hebben_we_al_takes_the_product_off(aiohttp_client, tmp_path):
    ctx = await planned(tmp_path)
    client = await aiohttp_client(make_app(ctx))
    r = await client.post(f"api/dish/{WO}/correct",
                          json={"ingredient": "300 g kipfilet", "kind": "pantry"})
    assert r.status == 200, await r.text()
    assert ctx.client.held() == {1: 2}
    body = await r.json()
    home = next(x for x in body["lines"] if x["ingredient"] == "300 g kipfilet")
    assert home["pantry"] is True and home["override"] == "pantry"


async def test_correct_refuses_what_it_cannot_check(aiohttp_client, tmp_path):
    ctx = await planned(tmp_path)
    client = await aiohttp_client(make_app(ctx))
    url = f"api/dish/{WO}/correct"
    r = await client.post(url, json={"ingredient": "1 rode ui", "kind": "weg"})
    assert r.status == 400
    r = await client.post(url, json={"ingredient": "1 rode ui", "kind": "product",
                                     "product_id": 7})
    assert r.status == 400 and "opnieuw" in (await r.json())["error"]
    r = await client.post(url, json={"ingredient": "1 banaan", "kind": "skip"})
    assert r.status == 404
    r = await client.get("api/products", params={"q": "x"})
    assert r.status == 400
    assert ctx.overrides.load() == {} and ctx.client.writes[-1][0] != "reduce"


async def test_correct_keeps_the_order_week_check(aiohttp_client, tmp_path):
    """Ruling 42: an order for another week gets nothing; the choice is still kept."""
    ctx = await planned(tmp_path)
    before = list(ctx.client.writes)
    ctx.client.order = ActiveOrder(55, {}, date(2026, 11, 20))
    ctx.client.search_results["kip"] = [p(9, "AH Kippendijen", "500 g")]
    client = await aiohttp_client(make_app(ctx))
    assert (await client.get("api/products", params={"q": "kip"})).status == 200
    r = await client.post(f"api/dish/{WO}/correct",
                          json={"ingredient": "300 g kipfilet", "kind": "product",
                                "product_id": 9})
    assert r.status == 200, await r.text()
    body = await r.json()
    assert body["held"] and ctx.client.writes == before
    assert "kipfilet" in ctx.overrides.load()


async def test_correct_while_writes_are_paused_writes_nothing(aiohttp_client, tmp_path):
    ctx = await planned(tmp_path)
    before = list(ctx.client.writes)
    ctx.health.failed("lijst", "vorm")
    ctx.health.failed("lijst", "vorm")
    assert ctx.health.writes_paused
    client = await aiohttp_client(make_app(ctx))
    r = await client.post(f"api/dish/{WO}/correct",
                          json={"ingredient": "300 g kipfilet", "kind": "skip"})
    assert r.status == 200
    assert (await r.json())["held"] == PAUSED_NOTE and ctx.client.writes == before
    check = (await (await client.get("api/week")).json())["check"]
    assert check["ok"] is False and check["blocked"] is True


async def test_correct_after_the_week_only_keeps_the_choice(aiohttp_client, tmp_path):
    ctx = await planned(tmp_path, now=datetime(2026, 11, 12, 9, 0))
    before = list(ctx.client.writes)
    client = await aiohttp_client(make_app(ctx))
    r = await client.post(f"api/dish/{WO}/correct",
                          json={"ingredient": "300 g kipfilet", "kind": "skip"})
    assert r.status == 200
    assert ctx.client.writes == before and "kipfilet" in ctx.overrides.load()
    assert ctx.ledger.load(W).plan == menu().to_dict()


async def test_corrections_are_serialised_with_the_other_writes(aiohttp_client, tmp_path):
    ctx = await planned(tmp_path)
    client = await aiohttp_client(make_app(ctx))
    rs = await asyncio.gather(
        client.post(f"api/dish/{WO}/correct", json={"ingredient": "300 g kipfilet",
                                                    "kind": "pantry"}),
        client.post(f"api/dish/{WO}/correct", json={"ingredient": "1 tl komijn",
                                                    "kind": "skip"}))
    assert [r.status for r in rs] == [200, 200]
    assert ctx.client.max_active_writes == 1
    assert set(ctx.overrides.load()) == {"kipfilet", "komijn"}


async def test_mijn_correcties_list_and_take_back(aiohttp_client, tmp_path):
    ctx = await planned(tmp_path)
    ctx.overrides.put("komijn", make_entry(SKIP, "1 tl komijn", None,
                                           datetime(2026, 11, 1, 8, 0)))
    client = await aiohttp_client(make_app(ctx))
    items = (await (await client.get("api/overrides")).json())["items"]
    assert items == [{"key": "komijn", "text": "1 tl komijn", "kind": "skip",
                      "product": None, "at": "2026-11-01T08:00"}]
    r = await client.post("api/overrides/remove", json={"key": "komijn"})
    assert r.status == 200 and (await r.json())["items"] == []
    r = await client.post("api/overrides/remove", json={"key": "komijn"})
    assert r.status == 404
    raw = json.loads(ctx.overrides.path.read_text(encoding="utf-8"))
    assert raw == {"schema": 1, "items": {}}


async def test_overrides_file_from_a_newer_add_on(aiohttp_client, tmp_path):
    ctx = await planned(tmp_path)
    ctx.overrides.path.write_text('{"schema": 9, "items": {}}', encoding="utf-8")
    main.scan_too_new(ctx)
    assert "overrides.json" in ctx.too_new
    client = await aiohttp_client(make_app(ctx))
    r = await client.post(f"api/dish/{WO}/correct",
                          json={"ingredient": "1 tl komijn", "kind": "skip"})
    assert r.status == 409


@pytest.fixture
def swaps(monkeypatch):
    async def fake_swap(client, plan, day, bonus, s, exclude):
        new = dish(day, 301, [1, 9], 1.5)
        return make_plan([d for d in plan.dishes if d.date != day] + [new])

    monkeypatch.setattr(main, "swap_dish", fake_swap)


async def test_swaps_and_removals_go_into_history(aiohttp_client, tmp_path, swaps):
    ctx = make_ctx(tmp_path)
    await apply(ctx.client, ctx.ledger, WeekLog(W), make_plan(
        [dish(WO, 101, [1, 2], 2.0), dish(DO, 102, [2, 3], 3.0)]))
    client = await aiohttp_client(make_app(ctx))
    assert (await client.post(f"api/dish/{WO}/swap")).status == 200
    assert (await client.post(f"api/dish/{WO}/back")).status == 200
    assert (await client.post(f"api/dish/{DO}/remove")).status == 200
    events = ctx.history.load()["2026-11-04"]
    assert [(e["date"], e["recipe_id"], e["action"]) for e in events] == [
        ("2026-11-04", 101, "swap"), ("2026-11-04", 301, "back"),
        ("2026-11-05", 102, "remove")]
    assert events[0]["title"] == "Recept 101" and events[0]["at"] == "2026-11-01T08:00"


async def test_a_broken_history_never_stops_the_swap(aiohttp_client, tmp_path, swaps):
    ctx = make_ctx(tmp_path)
    await apply(ctx.client, ctx.ledger, WeekLog(W), make_plan([dish(WO, 101, [1, 2], 2.0)]))
    ctx.history.path.write_text('{"schema": 7}', encoding="utf-8")
    client = await aiohttp_client(make_app(ctx))
    r = await client.post(f"api/dish/{WO}/swap")
    assert r.status == 200 and (await r.json())["recipe"]["id"] == 301
    assert "history.json" in ctx.too_new


async def test_products_search_does_not_wait_for_a_menu_round(aiohttp_client, tmp_path):
    from app.hass import BUSY
    ctx = await planned(tmp_path)
    ctx.in_round = True
    client = await aiohttp_client(make_app(ctx))
    r = await client.get("api/products", params={"q": "rode ui"})
    assert r.status == 409 and (await r.json())["error"] == BUSY
    assert ctx.client.searches == []


async def test_terugdraaien_puts_the_add_ons_own_choice_back_this_week(aiohttp_client,
                                                                        tmp_path):
    ctx = await planned(tmp_path)
    client = await aiohttp_client(make_app(ctx))
    r = await client.post(f"api/dish/{WO}/correct",
                          json={"ingredient": "300 g kipfilet", "kind": "pantry"})
    assert r.status == 200 and ctx.client.held() == {1: 2}
    ctx.client.search_results["kipfilet"] = [p(2, "AH Kipfilet", "300 g")]
    r = await client.post("api/overrides/remove", json={"key": "kipfilet"})
    assert r.status == 200, await r.text()
    body = await r.json()
    assert body["items"] == [] and body["held"] is None
    assert ctx.client.held() == {1: 2, 2: 1}  # back on the list, through apply
    assert ctx.client.searches == ["kipfilet"]
    log = ctx.ledger.load(W)
    assert log.added == {1: 2, 2: 1} and log.pending == {}
    d = await (await client.get(f"api/dish/{WO}")).json()
    line = next(x for x in d["lines"] if x["ingredient"] == "300 g kipfilet")
    assert line["product_id"] == 2 and line["override"] is None


async def test_terugdraaien_while_writes_are_paused_writes_nothing(aiohttp_client, tmp_path):
    ctx = await planned(tmp_path)
    client = await aiohttp_client(make_app(ctx))
    await client.post(f"api/dish/{WO}/correct",
                      json={"ingredient": "300 g kipfilet", "kind": "pantry"})
    before = list(ctx.client.writes)
    ctx.client.search_results["kipfilet"] = [p(2, "AH Kipfilet", "300 g")]
    ctx.health.failed("lijst", "vorm")
    ctx.health.failed("lijst", "vorm")
    r = await client.post("api/overrides/remove", json={"key": "kipfilet"})
    assert r.status == 200 and (await r.json())["held"] == PAUSED_NOTE
    assert ctx.client.writes == before and ctx.overrides.load() == {}
