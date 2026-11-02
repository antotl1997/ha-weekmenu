"""Vaste boodschappen in de bonus: history, suggestions, the API, the round and the message."""

import asyncio
import json
import re
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from app import ahclient, main
from app.ahclient import AhApiError, AhClient, BonusPeriod, Product
from app.hass import format_message
from app.ledger import WeekLog
from app.staples import Purchase, Staple, suggest_staples
from app.tokens import Tokens, TokenStore
from app.web import make_app
from app.writer import WriteResult, apply
from tests.fakes import SUNDAY, W, FakeAh, dish, make_ctx, make_plan

FIX = Path(__file__).parent / "fixtures"
BASE = "https://api.ah.nl"
TODAY = date(2026, 11, 1)


def fx(name):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


def bp(pid, title="AH Roomboter", mech="1 + 1 gratis", regular=3.0, now=None,
       cat="Zuivel, eieren", hq=None, orderable=True):
    return Product(pid, title, None, regular, regular if now is None else now, True, mech,
                   orderable, cat, hq_id=hq)


def buy(pid, days_ago, qty=1, source="online", hq=None):
    return Purchase(pid, f"P{pid}", qty, TODAY - timedelta(days=days_ago), source, hq_id=hq)


# --- suggest_staples -------------------------------------------------------------------


def test_two_purchases_in_two_months_qualifies():
    out = suggest_staples([buy(1, 10), buy(1, 61)], [bp(1)], {}, TODAY)
    assert [s.product.id for s in out] == [1]
    assert out[0].times == 2


def test_one_purchase_or_older_than_61_days_does_not():
    assert suggest_staples([buy(1, 5)], [bp(1)], {}, TODAY) == []
    assert suggest_staples([buy(2, 5), buy(2, 62)], [bp(2)], {}, TODAY) == []
    # bought often, but not in this week's bonus
    assert suggest_staples([buy(3, 5), buy(3, 6)], [bp(4)], {}, TODAY) == []


def test_online_and_store_purchases_both_count():
    # a receipt line only knows the hq id (AH article number), an order line both
    store = Purchase(0, "ROOMBOTER", 1, TODAY - timedelta(days=3), "winkel", hq_id=7001)
    online = buy(1, 20, hq=7001)
    out = suggest_staples([store, online], [bp(1, hq=7001)], {}, TODAY)
    assert len(out) == 1 and out[0].times == 2
    # store purchase alone, twice: counts too
    out = suggest_staples([store, Purchase(0, "ROOMBOTER", 1, TODAY - timedelta(days=30),
                                           "winkel", hq_id=7001)],
                          [bp(1, hq=7001)], {}, TODAY)
    assert out[0].times == 2
    # hq id 0 or missing never matches
    zero = [Purchase(0, "X", 1, TODAY - timedelta(days=d), "winkel", hq_id=0) for d in (1, 2)]
    assert suggest_staples(zero, [bp(5, hq=0)], {}, TODAY) == []


def test_menu_products_and_dinner_categories_excluded():
    gehakt = bp(2, "AH Rundergehakt", cat=None)  # in the menu
    vlees = bp(3, "AH Speklappen", cat="Vlees")
    groente = bp(4, "AH Courgette", cat="Groente, aardappelen")
    vis = bp(5, "AH Zalm", cat="Vis")
    maaltijd = bp(6, "AH Lasagne all uovo", cat="Maaltijden, salades")
    boter = bp(1, "AH Roomboter", cat="Zuivel, eieren")
    worst = bp(7, "AH Grillworst", cat="Vleeswaren", regular=2.0)  # beleg: stays
    bought = [buy(p, d) for p in (1, 2, 3, 4, 5, 6, 7) for d in (3, 20)]
    out = suggest_staples(bought, [gehakt, vlees, groente, vis, maaltijd, boter, worst],
                          {2: 1}, TODAY)
    assert [s.product.title for s in out] == ["AH Roomboter", "AH Grillworst"]


def test_usual_qty_is_median_rounded_up_then_deal_qty():
    out = suggest_staples([buy(1, 3, 1), buy(1, 10, 1), buy(1, 20, 2)],
                          [bp(1, mech="1 + 1 gratis")], {}, TODAY)
    assert out[0].usual_qty == 1 and out[0].qty == 2
    assert out[0].savings == 3.0
    out = suggest_staples([buy(1, 3, 1), buy(1, 10, 2)], [bp(1, mech="2e halve prijs")], {},
                          TODAY)
    assert out[0].usual_qty == 2 and out[0].qty == 2 and out[0].savings == 1.5


def test_no_saving_no_suggestion():
    flat = bp(1, mech=None)  # no deal, same price
    weird = bp(2, mech="100 GRAM VOOR 1.69")  # not computable: saves 0
    unorderable = bp(3, orderable=False)
    bought = [buy(p, d) for p in (1, 2, 3) for d in (3, 9)]
    assert suggest_staples(bought, [flat, weird, unorderable], {}, TODAY) == []


def test_sorted_by_savings_desc():
    cheap = bp(1, "Goedkoop", mech="2e halve prijs", regular=1.0)
    dear = bp(2, "Duur", mech="2e halve prijs", regular=8.0)
    mid = bp(3, "Middel", mech="40% korting", regular=5.0)
    bought = [buy(p, d) for p in (1, 2, 3) for d in (3, 9)]
    out = suggest_staples(bought, [cheap, dear, mid], {}, TODAY)
    assert [s.product.title for s in out] == ["Duur", "Middel", "Goedkoop"]
    assert out[0].savings > out[1].savings > out[2].savings


def test_staple_round_trips_through_dict():
    s = suggest_staples([buy(1, 3), buy(1, 4)], [bp(1, hq=9)], {}, TODAY)[0]
    assert Staple.from_dict(json.loads(json.dumps(s.to_dict()))) == s


# --- AhClient: purchase history from the real (anonymized) recordings -------------------


@pytest.fixture
def no_delay(monkeypatch):
    monkeypatch.setattr(ahclient, "CALL_DELAY", 0)


def make_client(tmp_path):
    store = TokenStore(tmp_path / "tokens.json")
    store.save(Tokens("a", "r", time.time() + 3600))
    return AhClient(store, httpx.AsyncClient())


def history_routes(respx_mock):
    """GraphQL by query name; order details by fake order id (n0 = 900001 ...)."""
    seen = []

    def graphql(request):
        body = json.loads(request.content)
        seen.append(body["query"])
        q = body["query"]
        if "orderFulfillments" in q:
            return httpx.Response(200, json=fx("history-graphql-fulfillments__closed.json")["body"])
        if "posReceiptsPage" in q:
            return httpx.Response(200, json=fx("history-graphql-receipts__limit50.json")["body"])
        if "posReceiptDetails" in q:
            n = int(body["variables"]["id"].split("-")[1]) - 1
            name = f"history-graphql-receipt-ids__n{n}.json"
            if not (FIX / name).exists():
                name = f"history-graphql-receipt__n{n}.json"
            return httpx.Response(200, json=fx(name)["body"])
        return httpx.Response(400, json={"errors": [{"message": "unexpected"}]})

    def details(request):
        oid = int(re.search(r"/order/v1/(\d+)/", request.url.path).group(1))
        return httpx.Response(200, json=fx(f"history-order-details__n{oid - 900001}.json")["body"])

    respx_mock.post(BASE + "/graphql").mock(side_effect=graphql)
    route = respx_mock.get(url__regex=r".*/order/v1/\d+/details-grouped-by-taxonomy").mock(
        side_effect=details)
    return seen, route


async def test_purchases_parses_orders_and_receipts(respx_mock, tmp_path, no_delay):
    seen, details = history_routes(respx_mock)
    client = make_client(tmp_path)
    out = await client.purchases(date(2026, 9, 1))

    # only the two deliveries and the one receipt since 1 September were read
    assert [c.request.url.path for c in details.calls] == [
        "/mobile-services/order/v1/900001/details-grouped-by-taxonomy",
        "/mobile-services/order/v1/900002/details-grouped-by-taxonomy",
    ]
    assert sum("posReceiptDetails" in q for q in seen) == 1
    assert all(q.lstrip().startswith("query") for q in seen)  # never a mutation

    online = [p for p in out if p.source == "online"]
    store = [p for p in out if p.source == "winkel"]
    assert len(online) == 9 + 12 and len(store) == 29
    assert Purchase(548041, "Lurpak Smeerbaar gezouten", 1, date(2026, 10, 28), "online",
                    hq_id=716583) in online
    skyr = sorted((p.day, p.qty) for p in online if p.product_id == 425572)
    assert skyr == [(date(2026, 10, 7), 4), (date(2026, 10, 28), 6)]
    # receipt lines carry the hq id; the title is the till's short name
    lurpak = next(p for p in store if p.hq_id == 768488)
    assert (lurpak.title, lurpak.qty, lurpak.day, lurpak.product_id) == (
        "LURPAK", 1, date(2026, 9, 28), 0)
    assert next(p for p in store if p.hq_id == 59574).qty == 3


async def test_purchases_skips_cancelled_orders(respx_mock, tmp_path, no_delay):
    history_routes(respx_mock)
    client = make_client(tmp_path)
    out = await client.purchases(date(2026, 7, 25))
    days = {p.day for p in out if p.source == "online"}
    assert date(2026, 8, 4) not in days  # 900004 was cancelled
    assert days == {date(2026, 10, 28), date(2026, 10, 7), date(2026, 8, 12), date(2026, 7, 29)}
    # a receipt line whose hq id was also delivered online gets its webshop id
    spinazie = [p for p in out if p.source == "winkel" and p.hq_id == 817384]
    assert spinazie and all(p.product_id == 224710 for p in spinazie)


async def test_previously_bought_bonus_parses_cards(respx_mock, tmp_path, no_delay):
    route = respx_mock.get(url__startswith=BASE + "/mobile-services/bonuspage/v2/section/previously-bought").respond(
        json=fx("history-previously-bought__p0.json")["body"])
    client = make_client(tmp_path)
    period = BonusPeriod(date(2026, 10, 26), date(2026, 11, 1), None, [])
    out = await client.previously_bought_bonus(period)
    assert "date=2026-10-26" in str(route.calls[0].request.url)
    assert "application=AHWEBSHOP" in str(route.calls[0].request.url)
    assert len(out) == 36
    lurpak = next(p for p in out if p.id == 172761)
    assert lurpak.hq_id == 768488 and lurpak.category == "Zuivel, eieren"
    assert lurpak.mechanism == "2e Halve Prijs" and lurpak.is_bonus
    assert next(p for p in out if p.id == 610208).hq_id is None  # hqId 0 means unknown


async def test_real_history_gives_real_suggestions(respx_mock, tmp_path, no_delay):
    """End to end on the recordings: receipts and orders against Eerder gekocht."""
    history_routes(respx_mock)
    respx_mock.get(url__startswith=BASE + "/mobile-services/bonuspage/v2/section/previously-bought").respond(
        json=fx("history-previously-bought__p1.json")["body"])
    client = make_client(tmp_path)
    period = BonusPeriod(date(2026, 11, 2), date(2026, 11, 8), None, [])
    bought = await client.purchases(TODAY - timedelta(days=61))
    bonus = await client.previously_bought_bonus(period)
    # the real history alone: every product was bought once in these 61 days
    assert suggest_staples(bought, bonus, {}, TODAY) == []
    # the receipt of 28 September has CHEDDAR RASP 2x (hq 600439); one more online
    # purchase of the same cheese makes it a staple, matched through the hq id
    extra = Purchase(454161, "AH Cheddar geraspte kaas", 2, date(2026, 10, 20), "online")
    out = suggest_staples(bought + [extra], bonus, {}, TODAY)
    assert [(s.product.id, s.product.title, s.times, s.usual_qty, s.qty, s.savings)
            for s in out] == [(454161, "AH Cheddar geraspte kaas", 2, 2, 2, 0.98)]
    assert out[0].product.mechanism == "2 voor 5.00"


# --- API ------------------------------------------------------------------------------


BOTER = bp(50, "AH Roomboter", mech="1 + 1 gratis", regular=3.0)


async def staples_ctx(tmp_path, ah=None):
    ctx = make_ctx(tmp_path, ah=ah)
    plan = make_plan([dish(W, 101, [1, 2], 2.0)])
    log = WeekLog(W)
    await apply(ctx.client, ctx.ledger, log, plan)
    log.staples = [Staple(BOTER, 3, 1, 2, 3.0).to_dict()]
    ctx.ledger.save(log)
    return ctx


async def test_api_add_staple_writes_once_and_logs(aiohttp_client, tmp_path):
    ctx = await staples_ctx(tmp_path)
    client = await aiohttp_client(make_app(ctx))
    r = await client.get("api/staples")
    items = await r.json()
    assert items == [{"product": "AH Roomboter", "product_id": 50, "times": 3, "qty": 2,
                      "mechanism": "1 + 1 gratis", "savings": 3.0, "added": False}]

    # two taps at once: one write
    r1, r2 = await asyncio.gather(client.post("api/staples/50"), client.post("api/staples/50"))
    assert r1.status == 200 and r2.status == 200
    assert [w for w in ctx.client.writes if 50 in w[1]] == [("list", {50: 2})]
    log = ctx.ledger.load(W)
    assert log.extras_added == {50: 2} and log.extras_savings == 3.0
    assert log.added == {1: 1, 2: 1}  # the menu's own log is untouched
    body = await r2.json()
    assert body["items"][0]["added"] is True and body["target"] == "list"

    r = await client.post("api/staples/50")  # later tap: still nothing new
    assert r.status == 200 and len([w for w in ctx.client.writes if 50 in w[1]]) == 1

    r = await client.post("api/staples/77")
    assert r.status == 404
    # only this or next menu week (Sunday 1 Nov: weeks of 28 Oct and 4 Nov)
    r = await client.post("api/staples/50?week=2026-11-11")
    assert r.status == 400
    r = await client.post("api/staples/50?week=2026-10-21")
    assert r.status == 400
    r = await client.get("api/week")
    week = await r.json()
    assert week["extras_savings"] == 3.0 and week["staples_error"] is None


async def test_api_add_staple_reports_actual_target(aiohttp_client, tmp_path):
    from app.ahclient import ActiveOrder
    ctx = await staples_ctx(tmp_path)  # the menu went on the list
    ctx.client.order = ActiveOrder(7, {})  # since then an order was opened
    client = await aiohttp_client(make_app(ctx))
    body = await (await client.post("api/staples/50")).json()
    assert body["target"] == "order" and ctx.client.order.items == {50: 2}


async def test_swap_that_settles_a_staple_updates_extras_savings(aiohttp_client, tmp_path,
                                                                 monkeypatch):
    ah = FakeAh(tmp_path, fail_on_call=2, fail_after=True)
    ctx = await staples_ctx(tmp_path, ah=ah)
    client = await aiohttp_client(make_app(ctx))
    assert (await client.post("api/staples/50")).status == 502  # unsure: pending
    assert ctx.ledger.load(W).extras_savings == 0.0

    async def fake_rescale(client_, plan, day, n):
        return make_plan([dish(W, 101, [1, 2], 2.0, persons=n)])

    monkeypatch.setattr(main, "rescale_dish", fake_rescale)
    r = await client.post(f"api/dish/{W.isoformat()}/persons", json={"persons": 3})
    assert r.status == 200
    log = ctx.ledger.load(W)
    assert log.extras_added == {50: 2} and log.extras_savings == 3.0
    assert len([w for w in ah.writes if 50 in w[1]]) == 1


async def test_api_staple_write_failure_is_not_retried(aiohttp_client, tmp_path):
    ah = FakeAh(tmp_path, fail_on_call=2, fail_after=True)  # menu: 2 writes, then the staple
    ctx = await staples_ctx(tmp_path, ah=ah)
    client = await aiohttp_client(make_app(ctx))
    r = await client.post("api/staples/50")
    assert r.status == 502
    log = ctx.ledger.load(W)
    assert 50 in log.pending and log.extras_added == {}
    r = await client.post("api/staples/50")  # outcome unsure: settled first, never blind
    assert r.status == 200
    assert len([w for w in ah.writes if 50 in w[1]]) == 1
    assert ctx.ledger.load(W).extras_added == {50: 2}


# --- the round ---------------------------------------------------------------------------


@pytest.fixture
def planned(monkeypatch):
    async def fake_build(client, bonus, s):
        return make_plan([dish(W, 101, [1, 2], 2.0)])

    monkeypatch.setattr(main, "build_plan", fake_build)


async def test_history_failure_does_not_break_menu(tmp_path, planned):
    ah = FakeAh(tmp_path)
    ah.raise_on["purchases"] = AhApiError("GET orders: HTTP 500", 500)
    ctx = make_ctx(tmp_path, ah=ah)
    out = await main.run_round(ctx, SUNDAY, "nu_maken")
    assert out.status == "done"
    log = ctx.ledger.load(W)
    assert log.added == {1: 1, 2: 1}
    assert log.staples == [] and log.staples_error
    assert "vaste boodschap" not in ctx.hass.notifications()[-1]["message"]
    assert ctx.error is None


def staples_ah(tmp_path):
    ah = FakeAh(tmp_path)
    ah.prev_bonus = [BOTER, bp(1, "Product 1")]  # 1 is in the menu
    day = SUNDAY.date()
    ah.history = [Purchase(50, "AH Roomboter", 1, day - timedelta(days=d), "online")
                  for d in (4, 30)] + [Purchase(1, "Product 1", 1, day - timedelta(days=d),
                                                "online") for d in (4, 30)]
    return ah


async def test_round_names_staples_count_in_the_message(tmp_path, planned):
    ah = staples_ah(tmp_path)
    ctx = make_ctx(tmp_path, ah=ah)
    out = await main.run_round(ctx, SUNDAY, "nu_maken")
    assert out.status == "done"
    assert ah.history_for == [SUNDAY.date() - timedelta(days=61)]
    log = ctx.ledger.load(W)
    assert [Staple.from_dict(s).product.id for s in log.staples] == [50]
    assert log.staples_error is None
    assert not any(50 in w[1] for w in ah.writes)  # a suggestion, never written by itself
    assert len(ctx.hass.notifications()) == 1
    assert "Ook 1 vaste boodschap in de bonus" in ctx.hass.notifications()[0]["message"]


async def test_slow_history_message_without_count(tmp_path, planned, monkeypatch):
    monkeypatch.setattr(main, "STAPLES_TIMEOUT", 0.05)
    ah = staples_ah(tmp_path)
    real = ah.purchases

    async def slow(since):
        await asyncio.sleep(1)
        return await real(since)

    ah.purchases = slow
    ctx = make_ctx(tmp_path, ah=ah)
    out = await main.run_round(ctx, SUNDAY, "nu_maken")
    assert out.status == "done" and ctx.error is None
    log = ctx.ledger.load(W)
    assert log.added == {1: 1, 2: 1}  # the menu is unaffected
    assert log.staples == [] and "duurde langer" in log.staples_error
    assert len(ctx.hass.notifications()) == 1  # one message, without a count
    msg = ctx.hass.notifications()[0]["message"]
    assert "vaste boodschap" not in msg and "Recept 101" in msg


# --- message ---------------------------------------------------------------------------


def test_message_mentions_staples_count():
    plan = make_plan([dish(W, 101, [1], 1.0)])
    _, text = format_message(plan, WriteResult("order"), "Niet deze week", None, staples=4)
    assert "Ook 4 vaste boodschappen in de bonus" in text
    _, text = format_message(plan, WriteResult("order"), "Niet deze week", None, staples=1)
    assert "Ook 1 vaste boodschap in de bonus" in text
    _, text = format_message(plan, WriteResult("order"), "Niet deze week", None)
    assert "vaste boodschap" not in text


async def test_token_refresh_happens_before_the_time_limit(tmp_path, planned, monkeypatch):
    """A refresh never starts inside the bounded window, so a timeout cannot cut it off."""
    monkeypatch.setattr(main, "STAPLES_TIMEOUT", 0.05)
    ah = staples_ah(tmp_path)
    order: list[str] = []

    async def slow_fresh():
        order.append("fresh start")
        await asyncio.sleep(0.2)  # longer than STAPLES_TIMEOUT: outside wait_for it finishes
        order.append("fresh done")

    real = ah.bonus_periods

    async def periods():
        order.append("periods")
        return await real()

    ah.ensure_fresh = slow_fresh
    ah.bonus_periods = periods
    ctx = make_ctx(tmp_path, ah=ah)
    out = await main.run_round(ctx, SUNDAY, "nu_maken")
    assert out.status == "done"
    # the menu's own reads come first; the staples step checks the token, then reads
    assert order[-3:] == ["fresh start", "fresh done", "periods"]
    log = ctx.ledger.load(W)
    assert log.staples_error is None and [s["product"]["id"] for s in log.staples] == [50]


async def test_staples_login_expired_is_reported_not_raised(tmp_path, planned):
    from app.ahclient import AhLoginExpired
    ah = staples_ah(tmp_path)
    ah.raise_on["ensure_fresh"] = AhLoginExpired("login expired", 400)
    ctx = make_ctx(tmp_path, ah=ah)
    out = await main.run_round(ctx, SUNDAY, "nu_maken")
    assert out.status == "done"  # the menu itself is on the list
    log = ctx.ledger.load(W)
    assert log.added == {1: 1, 2: 1} and "login expired" in log.staples_error
    assert "previously_bought_bonus" not in ah.calls
