import json
import time
from datetime import date
from pathlib import Path

import httpx
import pytest

from app import ahclient
from app.ahclient import (
    ActiveOrder,
    AhApiError,
    AhClient,
    AhNotJson,
    AhOrderMode,
    BonusPeriod,
    product_from_card,
    product_from_graphql,
)
from app.tokens import Tokens, TokenStore

FIX = Path(__file__).parent / "fixtures"
BASE = "https://api.ah.nl"
LIST_URL = BASE + "/mobile-services/shoppinglist/v2/items"
ORDER_GET = BASE + "/mobile-services/order/v1/summaries/active"
ORDER_PUT = BASE + "/mobile-services/order/v1/items"
SECTION = BASE + "/mobile-services/bonuspage/v2/section"
GRAPHQL = BASE + "/graphql"


@pytest.fixture(autouse=True)
def no_delay(monkeypatch):
    monkeypatch.setattr(ahclient, "CALL_DELAY", 0)


def fx(name):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


def make(tmp_path):
    store = TokenStore(tmp_path / "tokens.json")
    store.save(Tokens("a", "r", time.time() + 3600))
    http = httpx.AsyncClient()
    return AhClient(store, http), http


async def test_bonus_periods_parses_dates_and_next_visible(respx_mock, tmp_path):
    respx_mock.get(BASE + "/mobile-services/bonuspage/v3/metadata").respond(
        json=fx("bonus-metadata__v3.json")
    )
    client, http = make(tmp_path)
    periods = await client.bonus_periods()
    p0 = periods[0]
    assert p0.start == date(2026, 10, 26)
    assert p0.end == date(2026, 11, 1)
    assert p0.next_visible_from == date(2026, 10, 30)
    assert "Groente, aardappelen" in p0.categories
    assert "Vlees" in p0.categories
    assert len(p0.categories) == len(set(p0.categories))
    assert not any("Eerder" in c or "Uit de Bonusfolder" in c for c in p0.categories)
    await http.aclose()


def graphql_side_effect(request):
    body = json.loads(request.content)
    gid = body["variables"]["id"]
    assert body["variables"]["start"] == "2026-10-26"
    assert body["variables"]["end"] == "2026-11-01"
    f = FIX / f"graphql-bonuspromotions__p0-{gid}.json"
    if f.exists():
        return httpx.Response(200, json=json.loads(f.read_text(encoding="utf-8")))
    return httpx.Response(200, json={"data": {"bonusPromotions": []}})


PERIOD = BonusPeriod(
    date(2026, 10, 26), date(2026, 11, 1), None,
    ["Groente, aardappelen", "Vlees"],
)


async def test_bonus_offers_expands_group_deals_and_marks_bonus(respx_mock, tmp_path):
    respx_mock.get(SECTION, params={"category": "Groente, aardappelen"}).respond(
        json=fx("bonus-section__p0-groente-aardappelen.json")
    )
    respx_mock.get(SECTION, params={"category": "Vlees"}).respond(
        json=fx("bonus-section__p0-vlees.json")
    )
    respx_mock.post(GRAPHQL).mock(side_effect=graphql_side_effect)
    client, http = make(tmp_path)
    offers = await client.bonus_offers(PERIOD)
    by_id = {p.id: p for p in offers}
    soup = by_id[198386]  # inside group 816453, only reachable via GraphQL
    assert soup.title == "AH Tomatensoep verspakket"
    assert soup.is_bonus is True
    assert soup.now == 2.99 and soup.regular == 4.49
    assert soup.mechanism == "VOOR 2.99"
    assert soup.unit_size == "6 pers | 30 min"
    # GraphQL has no category; the planner needs the group's own (Taak 6)
    assert soup.category == "Groente, aardappelen"
    # a plain REST card keeps its own fields
    gehakt = by_id[619322]
    assert (gehakt.regular, gehakt.now, gehakt.mechanism) == (2.99, 1.49, "50% korting")
    assert gehakt.is_bonus and gehakt.orderable and gehakt.category
    assert client.skipped_categories == []
    assert len(by_id) == len(offers)
    await http.aclose()


async def test_bonus_offers_skips_broken_category_and_reports_it(respx_mock, tmp_path):
    respx_mock.get(SECTION, params={"category": "Groente, aardappelen"}).respond(
        json=fx("bonus-section__p0-groente-aardappelen.json")
    )
    respx_mock.get(SECTION, params={"category": "Vlees"}).respond(500)
    respx_mock.post(GRAPHQL).mock(side_effect=graphql_side_effect)
    client, http = make(tmp_path)
    offers = await client.bonus_offers(PERIOD)
    assert offers
    assert client.skipped_categories == ["Vlees"]
    await http.aclose()


def test_product_parsers_handle_both_shapes():
    card = product_from_card(
        {"webshopId": 5, "title": "X &amp; Y", "salesUnitSize": "1 kg", "currentPrice": None,
         "priceBeforeBonus": 2.5, "isBonus": False, "bonusMechanism": None}
    )
    assert (card.id, card.title, card.regular, card.now) == (5, "X & Y", 2.5, 2.5)
    assert card.orderable is True and card.category is None and card.mechanism is None
    gql = product_from_graphql(
        {"id": 6, "title": "Z", "salesUnitSize": "2 st", "priceV2": {"now": {"amount": 1.0}}}
    )
    assert gql.is_bonus and gql.regular == 1.0 and gql.now == 1.0 and gql.mechanism is None


async def test_search_products(respx_mock, tmp_path):
    respx_mock.get(BASE + "/mobile-services/product/search/v2").respond(
        json=fx("product-search__broccoli.json")
    )
    client, http = make(tmp_path)
    res = await client.search_products("broccoli")
    req = respx_mock.calls[0].request
    assert req.url.params["query"] == "broccoli" and req.url.params["size"] == "20"
    assert req.url.params["page"] == "0" and req.url.params["sortOn"] == "RELEVANCE"
    assert res[0].id == 484123 and res[0].is_bonus is False
    assert res[0].category == "Pasta, rijst, wereldkeuken"
    bonus = [p for p in res if p.title == "AH Biologisch Broccoli"][0]
    assert (bonus.regular, bonus.now, bonus.mechanism) == (2.79, 2.51, "10% KORTING")
    await http.aclose()


async def test_search_recipes(respx_mock, tmp_path):
    respx_mock.post(GRAPHQL).respond(json=fx("graphql-recipesearch__kip.json"))
    client, http = make(tmp_path)
    res = await client.search_recipes("kip")
    first = res[0]
    assert (first.id, first.cook, first.oven, first.wait, first.servings) == (
        1302918, 20, 40, 5, 2)
    assert first.slug == "rijsttraybake-met-kip-en-tempeh"
    assert first.courses == ["hoofdgerecht"]
    assert first.image.endswith("2048x1496_JPG.jpg")
    body = json.loads(respx_mock.calls[0].request.content)
    assert body["variables"]["query"] == {"searchText": "kip", "size": 30}
    await http.aclose()


async def test_get_recipe_decodes_entities(respx_mock, tmp_path):
    data = fx("graphql-recipe__1302918-s2.json")
    data["data"]["recipe"]["ingredients"][0]["text"] = "1&#189; teen knoflook"
    data["data"]["recipe"]["preparation"]["steps"][0] = "Meng &amp; roer"
    respx_mock.post(GRAPHQL).respond(json=data)
    client, http = make(tmp_path)
    r = await client.get_recipe(1302918, 2)
    assert r.ingredients[0][0] == "1½ teen knoflook"
    assert r.ingredients[1] == ("150 g scharrelkipfilets", "scharrelkipfilet")
    assert r.steps[0] == "Meng & roer"
    assert r.url == (
        "https://www.ah.nl/allerhande/recept/R-R1302918/rijsttraybake-met-kip-en-tempeh")
    assert (r.cook, r.oven, r.wait) == (20, None, None)
    assert r.cuisines == [] and r.servings == 2 and r.courses == ["hoofdgerecht"]
    assert r.image.endswith("2048x1496_JPG.jpg")
    vars_ = json.loads(respx_mock.calls[0].request.content)["variables"]
    assert vars_ == {"id": 1302918, "servings": 2}
    await http.aclose()


async def test_shopping_list_parses_products_only(respx_mock, tmp_path):
    respx_mock.get(LIST_URL).respond(json=fx("handmade-shoppinglist__items.json")["body"])
    client, http = make(tmp_path)
    assert await client.shopping_list() == {1235: 2, 777: 1}
    await http.aclose()


async def test_shopping_list_empty_real_fixture(respx_mock, tmp_path):
    respx_mock.get(LIST_URL).respond(json=fx("shoppinglist__items.json")["body"])
    client, http = make(tmp_path)
    assert await client.shopping_list() == {}
    await http.aclose()


async def test_add_to_list_sends_current_plus_added(respx_mock, tmp_path):
    respx_mock.get(LIST_URL).respond(
        json={"items": [{"quantity": 2, "productDetails": {"product": {"webshopId": 1235}}}]}
    )
    patch = respx_mock.patch(LIST_URL).respond(200)
    client, http = make(tmp_path)
    await client.add_to_shopping_list({1235: 1, 99: 3})
    assert patch.call_count == 1
    items = json.loads(patch.calls[0].request.content)["items"]
    by = {i["productId"]: i for i in items}
    assert by[1235]["quantity"] == 3 and by[99]["quantity"] == 3
    assert by[99] == {
        "description": "", "productId": 99, "quantity": 3, "type": "SHOPPABLE",
        "originCode": "PRD", "strikeThrough": False,
    }
    await http.aclose()


async def test_add_to_list_never_retries_failed_write(respx_mock, tmp_path):
    respx_mock.get(LIST_URL).respond(json={"items": []})
    patch = respx_mock.patch(LIST_URL).respond(500)
    client, http = make(tmp_path)
    with pytest.raises(AhApiError):
        await client.add_to_shopping_list({1: 1})
    assert patch.call_count == 1
    await http.aclose()


async def test_add_to_list_redirect_is_failure(respx_mock, tmp_path):
    respx_mock.get(LIST_URL).respond(json={"items": []})
    respx_mock.patch(LIST_URL).respond(302, headers={"Location": "https://x.invalid/"})
    client, http = make(tmp_path)
    with pytest.raises(AhApiError):
        await client.add_to_shopping_list({1: 1})
    await http.aclose()


async def test_shopping_list_in_order_mode_raises(respx_mock, tmp_path):
    respx_mock.get(LIST_URL).respond(
        400, json={"status": 400, "message": "Server in order mode"}
    )
    client, http = make(tmp_path)
    with pytest.raises(AhOrderMode) as e:
        await client.shopping_list()
    assert isinstance(e.value, AhApiError)
    assert "order mode" in str(e.value)
    await http.aclose()


async def test_add_to_list_in_order_mode_writes_nothing(respx_mock, tmp_path):
    respx_mock.get(LIST_URL).respond(400, text="Server in order mode")
    patch = respx_mock.patch(LIST_URL).respond(200)
    client, http = make(tmp_path)
    with pytest.raises(AhOrderMode):
        await client.add_to_shopping_list({1: 1})
    assert patch.call_count == 0
    await http.aclose()


async def test_shopping_list_other_error_is_plain_api_error(respx_mock, tmp_path):
    respx_mock.get(LIST_URL).respond(503, text="<html>down</html>")
    client, http = make(tmp_path)
    with pytest.raises(AhApiError) as e:
        await client.shopping_list()
    assert not isinstance(e.value, AhOrderMode) and e.value.status == 503
    await http.aclose()


async def test_shopping_list_html_200_is_not_json(respx_mock, tmp_path):
    respx_mock.get(LIST_URL).respond(200, text="<html>blocked</html>")
    client, http = make(tmp_path)
    with pytest.raises(AhNotJson):
        await client.shopping_list()
    await http.aclose()


async def test_active_order_parses_items(respx_mock, tmp_path):
    respx_mock.get(ORDER_GET).respond(json=fx("handmade-order-active__items.json")["body"])
    client, http = make(tmp_path)
    order = await client.active_order()
    assert order == ActiveOrder(123456, {1235: 2, 777: 1})
    assert respx_mock.calls[0].request.url.params["sortBy"] == "DEFAULT"
    await http.aclose()


async def test_active_order_none_when_no_order(respx_mock, tmp_path):
    f = fx("order-active__default.json")
    respx_mock.get(ORDER_GET).respond(f["status"], json=f["body"])
    client, http = make(tmp_path)
    assert await client.active_order() is None
    await http.aclose()


async def test_active_order_404_without_known_message_is_still_none(respx_mock, tmp_path):
    respx_mock.get(ORDER_GET).respond(404, json={"message": "iets anders"})
    client, http = make(tmp_path)
    assert await client.active_order() is None
    await http.aclose()


async def test_active_order_500_raises(respx_mock, tmp_path):
    respx_mock.get(ORDER_GET).respond(500)
    client, http = make(tmp_path)
    with pytest.raises(AhApiError):
        await client.active_order()
    await http.aclose()


async def test_add_to_order_sends_current_plus_added_with_order_header(respx_mock, tmp_path):
    respx_mock.get(ORDER_GET).respond(
        json={"id": 123456, "orderedProducts": [
            {"quantity": 2, "product": {"webshopId": 1235}}]}
    )
    put = respx_mock.put(ORDER_PUT).respond(200)
    client, http = make(tmp_path)
    order = ActiveOrder(123456, {1235: 2})
    await client.add_to_order(order, {1235: 1, 99: 3})
    req = put.calls[0].request
    assert req.headers["Appie-Current-Order-Id"] == "123456"
    assert req.url.params["sortBy"] == "DEFAULT"
    items = {i["productId"]: i for i in json.loads(req.content)["items"]}
    assert items[1235]["quantity"] == 3 and items[99]["quantity"] == 3
    assert items[99] == {
        "productId": 99, "quantity": 3, "originCode": "PRD", "description": "",
        "strikethrough": False,
    }
    await http.aclose()


async def test_add_to_order_never_lowers_quantity(respx_mock, tmp_path):
    put = respx_mock.put(ORDER_PUT).respond(200)
    client, http = make(tmp_path)
    with pytest.raises(ValueError):
        await client.add_to_order(ActiveOrder(1, {5: 4}), {5: -1})
    assert put.call_count == 0
    await http.aclose()


async def test_add_to_order_uses_fresh_read_when_order_changed_meanwhile(respx_mock, tmp_path):
    respx_mock.get(ORDER_GET).respond(
        json={"id": 123456, "orderedProducts": [
            {"quantity": 5, "product": {"webshopId": 1235}}]}
    )
    put = respx_mock.put(ORDER_PUT).respond(200)
    client, http = make(tmp_path)
    await client.add_to_order(ActiveOrder(123456, {1235: 2}), {1235: 1})
    items = json.loads(put.calls[0].request.content)["items"]
    assert items[0]["quantity"] == 6
    await http.aclose()


async def test_add_to_order_order_gone_writes_nothing(respx_mock, tmp_path):
    respx_mock.get(ORDER_GET).respond(404, json={"message": "Order does not exist"})
    put = respx_mock.put(ORDER_PUT).respond(200)
    client, http = make(tmp_path)
    with pytest.raises(AhApiError):
        await client.add_to_order(ActiveOrder(123456, {}), {1: 1})
    assert put.call_count == 0
    await http.aclose()


async def test_add_to_order_failure_not_retried(respx_mock, tmp_path):
    respx_mock.get(ORDER_GET).respond(json={"id": 1, "orderedProducts": []})
    put = respx_mock.put(ORDER_PUT).respond(502)
    client, http = make(tmp_path)
    with pytest.raises(AhApiError):
        await client.add_to_order(ActiveOrder(1, {}), {5: 1})
    assert put.call_count == 1
    await http.aclose()


def period_with(cats):
    return BonusPeriod(date(2026, 10, 26), date(2026, 11, 1), None, cats)


async def test_bonus_offers_html_stops_at_first_category(respx_mock, tmp_path):
    route = respx_mock.get(SECTION).respond(200, text="<html>blocked</html>")
    client, http = make(tmp_path)
    with pytest.raises(AhNotJson):
        await client.bonus_offers(period_with(["A", "B", "C", "D"]))
    assert route.call_count == 1
    await http.aclose()


async def test_bonus_offers_429_stops_immediately(respx_mock, tmp_path):
    route = respx_mock.get(SECTION).respond(429)
    client, http = make(tmp_path)
    with pytest.raises(AhApiError) as e:
        await client.bonus_offers(period_with(["A", "B", "C"]))
    assert e.value.status == 429 and route.call_count == 1
    await http.aclose()


async def test_bonus_offers_three_failures_in_a_row_raise(respx_mock, tmp_path):
    route = respx_mock.get(SECTION).respond(500)
    client, http = make(tmp_path)
    with pytest.raises(AhApiError, match="bonusfolder niet op te halen"):
        await client.bonus_offers(period_with(["A", "B", "C", "D", "E"]))
    assert route.call_count == 3
    await http.aclose()


async def test_bonus_offers_all_categories_failing_raises(respx_mock, tmp_path):
    respx_mock.get(SECTION).respond(500)
    client, http = make(tmp_path)
    with pytest.raises(AhApiError, match="bonusfolder niet op te halen"):
        await client.bonus_offers(period_with(["A", "B"]))
    await http.aclose()


async def test_bonus_offers_success_resets_failure_streak(respx_mock, tmp_path):
    ok = fx("bonus-section__p0-vlees.json")
    respx_mock.get(SECTION, params={"category": "A"}).respond(500)
    respx_mock.get(SECTION, params={"category": "B"}).respond(500)
    respx_mock.get(SECTION, params={"category": "C"}).respond(json=ok)
    respx_mock.get(SECTION, params={"category": "D"}).respond(500)
    respx_mock.post(GRAPHQL).mock(side_effect=graphql_side_effect)
    client, http = make(tmp_path)
    offers = await client.bonus_offers(period_with(["A", "B", "C", "D"]))
    assert offers and client.skipped_categories == ["A", "B", "D"]
    await http.aclose()


async def test_bonus_offers_failing_group_keeps_rest_of_category(respx_mock, tmp_path):
    sec = fx("bonus-section__p0-groente-aardappelen.json")
    respx_mock.get(SECTION).respond(json=sec)

    def side(request):
        if json.loads(request.content)["variables"]["id"] == "816453":
            return httpx.Response(500)
        return graphql_side_effect(request)

    respx_mock.post(GRAPHQL).mock(side_effect=side)
    client, http = make(tmp_path)
    offers = await client.bonus_offers(period_with(["Groente, aardappelen"]))
    ids = {p.id for p in offers}
    assert 198386 not in ids  # inside the failed group 816453
    assert 619322 not in ids  # vlees product is not in this category
    assert ids, "rest of the category must survive"
    assert client.skipped_groups == ["816453"]
    assert client.skipped_categories == []
    await http.aclose()


async def test_bonus_offers_group_html_stops(respx_mock, tmp_path):
    sec = fx("bonus-section__p0-groente-aardappelen.json")
    respx_mock.get(SECTION).respond(json=sec)
    route = respx_mock.post(GRAPHQL).respond(200, text="<html>x</html>")
    client, http = make(tmp_path)
    with pytest.raises(AhNotJson):
        await client.bonus_offers(period_with(["Groente, aardappelen"]))
    assert route.call_count == 1
    await http.aclose()


async def test_bonus_offers_caches_group_expansion_across_categories(respx_mock, tmp_path):
    sec = fx("bonus-section__p0-groente-aardappelen.json")
    respx_mock.get(SECTION).respond(json=sec)
    route = respx_mock.post(GRAPHQL).mock(side_effect=graphql_side_effect)
    client, http = make(tmp_path)
    await client.bonus_offers(period_with(["A", "B"]))
    ids = {e["bonusGroup"]["id"] for e in sec["bonusGroupOrProducts"] if e.get("bonusGroup")}
    assert route.call_count == len(ids)
    await http.aclose()


async def test_bonus_offers_pauses_between_calls(respx_mock, tmp_path, monkeypatch):
    slept = []

    async def fake_sleep(s):
        slept.append(s)

    monkeypatch.setattr(ahclient, "CALL_DELAY", 0.2)
    monkeypatch.setattr(ahclient.asyncio, "sleep", fake_sleep)
    respx_mock.get(SECTION).respond(json={"bonusGroupOrProducts": []})
    client, http = make(tmp_path)
    await client.bonus_offers(period_with(["A", "B", "C"]))
    assert slept == [0.2, 0.2]
    await http.aclose()


async def test_add_to_order_on_before_gets_fresh_quantities_before_put(respx_mock, tmp_path):
    respx_mock.get(ORDER_GET).respond(
        json={"id": 123456, "orderedProducts": [{"quantity": 4, "product": {"webshopId": 1235}}]}
    )
    put = respx_mock.put(ORDER_PUT).respond(200)
    client, http = make(tmp_path)
    seen = []
    await client.add_to_order(
        ActiveOrder(123456, {1235: 1}), {1235: 1, 99: 3},
        on_before=lambda cur: seen.append((dict(cur), put.call_count)),
    )
    assert seen == [({1235: 4, 99: 0}, 0)]
    await http.aclose()


async def test_add_to_shopping_list_on_before_gets_current_before_patch(respx_mock, tmp_path):
    respx_mock.get(LIST_URL).respond(json={"items": [
        {"quantity": 2, "productDetails": {"product": {"webshopId": 7}}}]})
    patch = respx_mock.patch(LIST_URL).respond(200)
    client, http = make(tmp_path)
    seen = []
    await client.add_to_shopping_list(
        {7: 1}, on_before=lambda cur: seen.append((dict(cur), patch.call_count)))
    assert seen == [({7: 2}, 0)]
    await http.aclose()
