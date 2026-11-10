"""Tab Aanbiedingen: the folder per category, one tap adds a loose offer, savings."""

import asyncio
from datetime import date

from app import main
from app.ahclient import ActiveOrder, Product
from app.deals import qty_for_deal, savings
from app.ledger import WeekLog
from app.web import make_app
from app.writer import apply, apply_extras
from tests.fakes import W, FakeAh, dish, make_ctx, make_plan

PERIOD = date(2026, 11, 2)  # the folder of the menu week W (Wednesday 4 Nov)


def op(pid, title, cat, mech="1 + 1 gratis", regular=3.0, now=None, orderable=True,
       image="https://example.invalid/p.jpg"):
    return Product(pid, title, "250 g", regular, regular if now is None else now,
                   True, mech, orderable, cat, image=image)


KAAS = op(50, "AH Jonge kaas", "Kaas, vleeswaren")
BOTER = op(51, "AH Roomboter", "Zuivel, eieren", mech="2e halve prijs")
MELK = op(52, "AH Halfvolle melk", "Zuivel, eieren", mech="25% korting")
ZEEP = op(53, "Dove <b>zeep</b>", None, mech="2 voor 5.00", regular=3.0)
ZUUR = op(54, "AH Zuurkool", "Zuivel, eieren", orderable=False)
FOLDER = [MELK, ZEEP, KAAS, BOTER, ZUUR]


class OfferAh(FakeAh):
    async def bonus_offers(self, period):
        self._maybe_raise("bonus_offers")
        self.offers_for.append(period)
        await asyncio.sleep(0.01)
        return list(FOLDER)


def ctx_with_cache(tmp_path, **kw):
    ctx = make_ctx(tmp_path, ah=OfferAh(tmp_path, **kw))
    ctx.bonus_cache = {PERIOD: list(FOLDER)}
    return ctx


# --- reading -----------------------------------------------------------------------------


async def test_offers_are_grouped_by_category_and_searchable(aiohttp_client, tmp_path):
    ctx = ctx_with_cache(tmp_path)
    client = await aiohttp_client(make_app(ctx))
    r = await client.get("api/offers")
    assert r.status == 200
    body = await r.json()
    assert [c["name"] for c in body["categories"]] == [
        "Kaas, vleeswaren", "Zuivel, eieren", "Overig"]
    zuivel = body["categories"][1]["items"]
    assert [i["title"] for i in zuivel] == ["AH Halfvolle melk", "AH Roomboter", "AH Zuurkool"]
    assert zuivel[1] == {
        "id": 51, "title": "AH Roomboter", "mechanism": "2e halve prijs", "price_now": 3.0,
        "price_regular": 3.0, "unit_size": "250 g", "image": "https://example.invalid/p.jpg",
        "added_qty": 0, "orderable": True}
    assert ctx.client.offers_for == []  # the cache was used

    body = await (await client.get("api/offers?q=BOTER")).json()  # not case sensitive, title only
    assert [(c["name"], [i["id"] for i in c["items"]]) for c in body["categories"]] == [
        ("Zuivel, eieren", [51])]
    body = await (await client.get("api/offers?q=zuivel")).json()  # a category name is no hit
    assert body["categories"] == []
    body = await (await client.get("api/offers?category=kaas, vleeswaren")).json()
    assert [c["name"] for c in body["categories"]] == ["Kaas, vleeswaren"]


async def test_empty_cache_is_filled_once_under_the_lock(aiohttp_client, tmp_path):
    ctx = make_ctx(tmp_path, ah=OfferAh(tmp_path))
    client = await aiohttp_client(make_app(ctx))
    rs = await asyncio.gather(client.get("api/offers"), client.get("api/offers"))
    assert [r.status for r in rs] == [200, 200]
    assert len(ctx.client.offers_for) == 1
    assert (await rs[1].json())["categories"]


async def test_offers_only_for_this_or_the_next_menu_week(aiohttp_client, tmp_path):
    ctx = ctx_with_cache(tmp_path)
    client = await aiohttp_client(make_app(ctx))
    assert (await client.get("api/offers?week=2026-11-11")).status == 400
    assert (await client.post("api/offers/50?week=2026-11-11")).status == 400
    assert (await client.post("api/offers/50?week=2026-10-21")).status == 400
    assert ctx.client.writes == []


# --- adding ------------------------------------------------------------------------------


async def test_tap_writes_once_with_the_quantity_the_deal_asks(aiohttp_client, tmp_path):
    ctx = ctx_with_cache(tmp_path)
    client = await aiohttp_client(make_app(ctx))
    r = await client.post("api/offers/50")
    assert r.status == 200
    body = await r.json()
    assert body["added_qty"] == 2 and body["target"] == "list"
    assert qty_for_deal(KAAS, 1) == 2
    assert ctx.client.writes == [("list", {50: 2})]
    log = ctx.ledger.load(W)
    assert log.offers_added == {50: 2} and log.offers_savings == savings(KAAS, 2) == 3.0
    assert log.extras_added == {} and log.added == {} and log.pending == {}
    items = (await (await client.get("api/offers")).json())["categories"][0]["items"]
    assert items[0]["id"] == 50 and items[0]["added_qty"] == 2


async def test_a_single_unit_deal_adds_one(aiohttp_client, tmp_path):
    ctx = ctx_with_cache(tmp_path)
    ctx.client.order = ActiveOrder(9, {})
    client = await aiohttp_client(make_app(ctx))
    body = await (await client.post("api/offers/52")).json()  # 25% korting
    assert ctx.client.writes == [("order", {52: 1})] and body["target"] == "order"
    assert ctx.ledger.load(W).offers_savings == 0.75


async def test_double_tap_adds_nothing_twice(aiohttp_client, tmp_path):
    ctx = ctx_with_cache(tmp_path)
    client = await aiohttp_client(make_app(ctx))
    rs = await asyncio.gather(client.post("api/offers/50"), client.post("api/offers/50"))
    assert [r.status for r in rs] == [200, 200]
    assert (await client.post("api/offers/50")).status == 200  # a later tap too
    assert ctx.client.writes == [("list", {50: 2})]
    assert ctx.ledger.load(W).offers_savings == 3.0


async def test_a_staple_already_added_is_not_added_again(aiohttp_client, tmp_path):
    ctx = ctx_with_cache(tmp_path)
    ctx.ledger.save(WeekLog(W, extras_added={50: 2}))
    client = await aiohttp_client(make_app(ctx))
    body = await (await client.post("api/offers/50")).json()
    assert ctx.client.writes == [] and body["added_qty"] == 2


async def test_unknown_or_unorderable_product_is_refused(aiohttp_client, tmp_path):
    ctx = ctx_with_cache(tmp_path)
    client = await aiohttp_client(make_app(ctx))
    assert (await client.post("api/offers/999")).status == 404
    assert (await client.post("api/offers/abc")).status == 400
    assert (await client.post("api/offers/54")).status == 409
    assert ctx.client.writes == []


async def test_unsure_write_is_settled_first_never_repeated(aiohttp_client, tmp_path):
    ah = OfferAh(tmp_path, fail_on_call=0, fail_after=True)  # AH applied it, the call failed
    ctx = make_ctx(tmp_path, ah=ah)
    ctx.bonus_cache = {PERIOD: list(FOLDER)}
    client = await aiohttp_client(make_app(ctx))
    r = await client.post("api/offers/50")
    assert r.status == 502  # AH failed after applying it: unsure, kept in pending
    log = ctx.ledger.load(W)
    assert log.pending[50]["kind"] == "offer" and log.offers_added == {}
    assert log.offers_savings == 0.0
    r = await client.post("api/offers/50")  # settled by reading AH, not written again
    assert r.status == 200
    assert ah.writes == [("list", {50: 2})]
    log = ctx.ledger.load(W)
    assert log.offers_added == {50: 2} and log.offers_savings == 3.0 and log.pending == {}


async def test_write_ahead_entry_is_on_disk_before_the_write(tmp_path):
    ah = OfferAh(tmp_path)
    ctx = make_ctx(tmp_path, ah=ah)
    seen = []
    real = ctx.ledger.save

    def spy(log):
        seen.append({k: dict(v) for k, v in log.pending.items()})
        real(log)

    ctx.ledger.save = spy
    log = WeekLog(W)
    await apply_extras(ah, ctx.ledger, log, {50: 2}, {50: "x"}, kind="offer",
                       savings={50: 3.0})
    assert any(s.get(50, {}).get("kind") == "offer" and s[50]["delta"] == 2 for s in seen)
    assert log.offers_added == {50: 2} and log.pending == {}


# --- never cleaned -----------------------------------------------------------------------


async def test_loose_offers_survive_a_swap_and_a_new_menu(tmp_path):
    ah = OfferAh(tmp_path)
    ctx = make_ctx(tmp_path, ah=ah)
    old = make_plan([dish(W, 101, [1, 2], 2.0)])
    log = WeekLog(W)
    await apply(ah, ctx.ledger, log, old)
    # a loose offer on a product that is not in the menu, and one that is (product 1)
    await apply_extras(ah, ctx.ledger, log, {99: 2, 1: 2}, kind="offer",
                       savings={99: 1.0, 1: 1.0})
    assert ah.listed == {1: 3, 2: 1, 99: 2}
    new = make_plan([dish(W, 102, [3], 1.0)])
    res = await apply(ah, ctx.ledger, log, new, replaced=old, today=date(2026, 11, 1))
    # only the menu's own share of products 1 and 2 went; the offers stayed
    assert ah.listed == {1: 2, 3: 1, 99: 2}
    assert res.removed == {1: 1, 2: 1}
    assert log.offers_added == {99: 2, 1: 2} and log.offers_savings == 2.0
    assert 99 not in log.added and not any(w[1].get(99, 0) < 0 for w in ah.writes)


# --- savings -----------------------------------------------------------------------------


async def test_week_savings_are_menu_plus_extras_plus_offers(aiohttp_client, tmp_path):
    ctx = ctx_with_cache(tmp_path)
    plan = make_plan([dish(W, 101, [1, 2], 5.0)])
    ctx.ledger.save(WeekLog(W, plan=plan.to_dict(), savings=5.0, extras_savings=1.5,
                            offers_savings=2.0, counted=True))
    client = await aiohttp_client(make_app(ctx))
    week = await (await client.get("api/week")).json()
    assert week["savings"] == 5.0 and week["extras_savings"] == 1.5
    assert week["offers_savings"] == 2.0
    s = await (await client.get("api/savings")).json()
    assert s["weeks"] == [{"week": W.isoformat(), "savings": 8.5, "counted": True}]


async def test_total_counts_the_offers_once(tmp_path):
    ctx = ctx_with_cache(tmp_path)
    plan = make_plan([dish(W, 101, [1, 2], 5.0)])
    log = WeekLog(W, plan=plan.to_dict(), savings=5.0, extras_savings=1.0)
    ctx.ledger.save(log)
    today = date(2026, 11, 5)
    await main.update_total(ctx, today)
    assert ctx.hass.totals()[-1] == 6.0
    log.offers_savings = 3.0  # an offer tapped after the week started
    ctx.ledger.save(log)
    await main.update_total(ctx, today)
    assert ctx.hass.totals()[-1] == 9.0
    await main.update_total(ctx, today)
    assert ctx.hass.totals()[-1] == 9.0


def test_old_log_without_offer_fields_loads():
    log = WeekLog.from_dict({"week": "2026-11-04", "added": {"1": 2}})
    assert log.offers_added == {} and log.offers_savings == 0.0
    back = WeekLog.from_dict(WeekLog(W, offers_added={5: 2}, offers_savings=1.25).to_dict())
    assert back.offers_added == {5: 2} and back.offers_savings == 1.25


# --- staples and offers never both buy it (Ruling 41) -------------------------------------


async def test_staple_route_skips_a_product_already_added_as_offer(aiohttp_client, tmp_path):
    from app.staples import Staple
    ctx = ctx_with_cache(tmp_path)
    ctx.ledger.save(WeekLog(W, offers_added={50: 2}, offers_savings=3.0,
                            staples=[Staple(KAAS, 3, 1, 2, 3.0).to_dict()]))
    client = await aiohttp_client(make_app(ctx))
    items = await (await client.get("api/staples")).json()
    assert items[0]["added"] is True  # counted as added
    r = await client.post("api/staples/50")
    assert r.status == 200 and ctx.client.writes == []
    assert ctx.ledger.load(W).extras_added == {}


async def test_offer_then_staple_and_staple_then_offer_buy_once(aiohttp_client, tmp_path):
    from app.staples import Staple
    ctx = ctx_with_cache(tmp_path)
    ctx.ledger.save(WeekLog(W, staples=[Staple(KAAS, 3, 1, 2, 3.0).to_dict()]))
    client = await aiohttp_client(make_app(ctx))
    assert (await client.post("api/offers/50")).status == 200
    assert (await client.post("api/staples/50")).status == 200
    assert ctx.client.writes == [("list", {50: 2})]
    ctx.ledger.save(WeekLog(W, extras_added={51: 2}))
    assert (await client.post("api/offers/51")).status == 200
    assert ctx.client.writes == [("list", {50: 2})]


# --- the current folder standing in for the week's own ----------------------------------


async def test_fallback_folder_is_remembered_and_flagged(aiohttp_client, tmp_path):
    from tests.fakes import CURRENT
    ah = OfferAh(tmp_path, periods=(CURRENT,))  # the folder of the menu week is not out
    ctx = make_ctx(tmp_path, ah=ah)
    client = await aiohttp_client(make_app(ctx))
    bodies = [await (await client.get("api/offers")).json() for _ in range(3)]
    assert all(b["categories"] for b in bodies)
    assert bodies[0]["period_warning"] == (
        "Dit is de huidige folder; de folder van je bezorgweek is er nog niet.")
    assert ah.calls.count("bonus_periods") == 1 and ah.calls.count("bonus_offers") == 1
    # once the real folder is seen (hourly look), the mapping is dropped
    ctx.ledger.save(WeekLog(W, new_folder=date(2026, 11, 2)))
    ah.periods = [CURRENT, ah.periods[0].__class__(date(2026, 11, 2), date(2026, 11, 8), None,
                                                   ["Vlees"])]
    body = await (await client.get("api/offers")).json()
    assert body["period_warning"] is None and ah.calls.count("bonus_offers") == 2


async def test_own_folder_has_no_warning(aiohttp_client, tmp_path):
    ctx = ctx_with_cache(tmp_path)
    client = await aiohttp_client(make_app(ctx))
    assert (await (await client.get("api/offers")).json())["period_warning"] is None
