"""v0.7: ApiHealth, shape checks, 403 as a block and the app version as an option."""
import json
import logging
import time
from datetime import date
from pathlib import Path

import httpx
import pytest

from app import ahclient
from app.ahclient import (
    AhApiError,
    AhClient,
    AhLoginExpired,
    AhNotJson,
    AhOrderMode,
    AhShapeError,
    BonusPeriod,
    health_kind,
    is_blocking,
)
from app.health import PAUSE_AFTER, ApiHealth
from app.tokens import Tokens, TokenStore

FIX = Path(__file__).parent / "fixtures"
BASE = "https://api.ah.nl"
META = BASE + "/mobile-services/bonuspage/v3/metadata"
LIST_URL = BASE + "/mobile-services/shoppinglist/v2/items"
ORDER_GET = BASE + "/mobile-services/order/v1/summaries/active"
SECTION = BASE + "/mobile-services/bonuspage/v2/section"
SEARCH = BASE + "/mobile-services/product/search/v2"
GRAPHQL = BASE + "/graphql"


@pytest.fixture(autouse=True)
def no_delay(monkeypatch):
    monkeypatch.setattr(ahclient, "CALL_DELAY", 0)


def fx(name):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


def make(tmp_path, **kw):
    store = TokenStore(tmp_path / "tokens.json")
    store.save(Tokens("a", "r", time.time() + 3600))
    http = httpx.AsyncClient()
    return AhClient(store, http, **kw), http


# --- ApiHealth on its own ----------------------------------------------------------------


def test_two_counted_failures_in_a_row_pause_writes():
    h = ApiHealth()
    assert PAUSE_AFTER == 2
    h.failed("lijst", "vorm")
    assert not h.writes_paused
    h.failed("lijst", "vorm")
    assert h.writes_paused and h.paused_by == "lijst"


def test_a_success_in_between_resets_the_count():
    h = ApiHealth()
    h.failed("bonus", "vorm")
    h.ok("bonus")
    h.failed("bonus", "vorm")
    assert not h.writes_paused and h.fails["bonus"] == 1


def test_failures_on_different_endpoints_do_not_add_up():
    h = ApiHealth()
    h.failed("bonus", "vorm")
    h.failed("lijst", "vorm")
    assert not h.writes_paused


def test_a_full_read_round_resumes_writing():
    h = ApiHealth()
    h.failed("lijst", "vorm")
    h.failed("lijst", "vorm")
    h.ok("bonus")
    assert h.writes_paused  # bonus alone is not a read round
    h.ok("lijst")
    assert not h.writes_paused and h.fails["lijst"] == 0


def test_a_read_round_must_include_the_endpoint_that_paused():
    h = ApiHealth()
    h.failed("lijst", "vorm")
    h.failed("lijst", "vorm")
    h.ok("bonus")
    h.ok("bestelling")  # a 404 without an order: the list was never read
    assert h.writes_paused
    h.failed("lijst", "vorm")  # a failure while paused starts the round over
    h.ok("lijst")
    assert h.writes_paused
    h.ok("bonus")
    assert not h.writes_paused


def test_manual_resume_clears_the_counts():
    h = ApiHealth()
    h.failed("recept", "vorm")
    h.failed("recept", "vorm")
    h.resume("knop")
    assert not h.writes_paused and h.paused_by is None
    h.failed("recept", "vorm")
    assert not h.writes_paused  # one failure after the button is not two


def test_view_has_no_bodies_only_kinds_and_counts():
    h = ApiHealth()
    h.failed("bestelling", "HTTP 403")
    v = h.view()
    assert v["endpoints"]["bestelling"] == {"fouten_achter_elkaar": 1, "laatste_fout": "HTTP 403"}
    assert v["writes_paused"] is False and set(v["endpoints"]) == {
        "bonus", "recept", "product", "lijst", "bestelling"}


@pytest.mark.parametrize("endpoint,exc,kind", [
    ("lijst", AhShapeError("x", 200), "vorm"),
    ("lijst", AhNotJson("x", 200), "geen JSON"),
    ("bonus", AhApiError("x", 400), "HTTP 400"),
    ("bonus", AhApiError("x", 403), "HTTP 403"),
    ("bonus", AhApiError("x", 429), "HTTP 429"),
    ("lijst", AhApiError("x", 404), "HTTP 404"),
    ("bestelling", AhApiError("x", 404), None),  # no active order: not a failure
    ("lijst", AhLoginExpired("x", 401), None),
    ("lijst", AhOrderMode("x", 400), None),  # a known state, not a change
    ("bonus", AhApiError("x", 500), None),
    ("bonus", AhApiError("x", 0), None),
])
def test_what_counts_as_a_failure(endpoint, exc, kind):
    assert health_kind(endpoint, exc) == kind


def test_403_and_429_block():
    assert is_blocking(AhApiError("x", 403)) and is_blocking(AhApiError("x", 429))
    assert not is_blocking(AhShapeError("x", 200)) and not is_blocking(AhApiError("x", 500))


# --- shape checks in the client --------------------------------------------------------------


async def test_bonus_metadata_without_periods_is_a_shape_error(respx_mock, tmp_path):
    respx_mock.get(META).respond(json={"tijdvakken": []})
    client, http = make(tmp_path)
    with pytest.raises(AhShapeError):
        await client.bonus_periods()
    assert client.health.fails["bonus"] == 1
    await http.aclose()


async def test_bonus_period_with_a_renamed_date_is_a_shape_error(respx_mock, tmp_path):
    meta = fx("bonus-metadata__v3.json")
    meta["periods"][0]["startDate"] = meta["periods"][0].pop("bonusStartDate")
    respx_mock.get(META).respond(json=meta)
    client, http = make(tmp_path)
    with pytest.raises(AhShapeError):
        await client.bonus_periods()
    await http.aclose()


async def test_real_fixtures_pass_the_shape_checks(respx_mock, tmp_path):
    respx_mock.get(META).respond(json=fx("bonus-metadata__v3.json"))
    respx_mock.get(LIST_URL).respond(json=fx("handmade-shoppinglist__items.json")["body"])
    respx_mock.get(ORDER_GET).respond(json=fx("handmade-order-active__items.json")["body"])
    respx_mock.get(SEARCH).respond(json=fx("product-search__biologische-citroen.json"))
    respx_mock.post(GRAPHQL).respond(json=fx("graphql-recipesearch__kip.json"))
    client, http = make(tmp_path)
    assert await client.bonus_periods()
    assert await client.shopping_list()
    assert await client.active_order()
    assert await client.search_products("ui")
    assert await client.search_recipes("kip")
    assert client.health.last_ok is not None
    assert all(n == 0 for n in client.health.fails.values())
    await http.aclose()


async def test_list_items_renamed_is_a_shape_error_and_twice_pauses(respx_mock, tmp_path):
    body = fx("handmade-shoppinglist__items.json")["body"]
    body["entries"] = body.pop("items")
    respx_mock.get(LIST_URL).respond(json=body)
    client, http = make(tmp_path)
    for _ in range(2):
        with pytest.raises(AhShapeError):
            await client.shopping_list()
    assert client.health.writes_paused and client.health.paused_by == "lijst"
    await http.aclose()


async def test_list_quantity_of_the_wrong_type_is_a_shape_error(respx_mock, tmp_path):
    body = fx("handmade-shoppinglist__items.json")["body"]
    body["items"][0]["quantity"] = {"amount": 2}
    respx_mock.get(LIST_URL).respond(json=body)
    client, http = make(tmp_path)
    with pytest.raises(AhShapeError):
        await client.shopping_list()
    await http.aclose()


async def test_order_without_id_is_a_shape_error(respx_mock, tmp_path):
    body = fx("handmade-order-active__items.json")["body"]
    body["orderId"] = body.pop("id")
    respx_mock.get(ORDER_GET).respond(json=body)
    client, http = make(tmp_path)
    with pytest.raises(AhShapeError):
        await client.active_order()
    assert client.health.fails["bestelling"] == 1
    await http.aclose()


async def test_404_on_the_order_is_logged_with_aim_and_reason(respx_mock, tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="weekmenu.ahclient")
    f = fx("order-active__default.json")
    respx_mock.get(ORDER_GET).respond(f["status"], json=f["body"])
    client, http = make(tmp_path)
    assert await client.active_order() is None
    assert "geen actieve bestelling, lijst gebruikt" in caplog.text
    assert client.health.fails["bestelling"] == 0
    await http.aclose()


async def test_product_search_without_products_list_is_a_shape_error(respx_mock, tmp_path):
    respx_mock.get(SEARCH).respond(json={"producten": []})
    client, http = make(tmp_path)
    with pytest.raises(AhShapeError):
        await client.search_products("ui")
    assert client.health.fails["product"] == 1
    await http.aclose()


async def test_recipe_graphql_errors_count_as_a_change(respx_mock, tmp_path):
    respx_mock.post(GRAPHQL).respond(json={"errors": [{"message": "Cannot query field"}]})
    client, http = make(tmp_path)
    with pytest.raises(AhShapeError):
        await client.search_recipes("kip")
    with pytest.raises(AhShapeError):
        await client.get_recipe(1, 2)
    assert client.health.writes_paused and client.health.paused_by == "recept"
    await http.aclose()


async def test_one_broken_category_is_still_skipped_and_the_folder_counts_as_read(
        respx_mock, tmp_path):
    respx_mock.get(SECTION, params={"category": "A"}).respond(json={"iets": []})
    respx_mock.get(SECTION, params={"category": "B"}).respond(
        json=fx("bonus-section__p0-vlees.json"))
    respx_mock.post(GRAPHQL).respond(json={"data": {"bonusPromotions": []}})
    client, http = make(tmp_path)
    offers = await client.bonus_offers(
        BonusPeriod(date(2026, 10, 26), date(2026, 11, 1), None, ["A", "B"]))
    assert offers and client.skipped_categories == ["A"]
    assert client.health.fails["bonus"] == 0
    await http.aclose()


async def test_a_folder_of_only_broken_categories_is_a_shape_error(respx_mock, tmp_path):
    respx_mock.get(SECTION).respond(json={"iets": []})
    client, http = make(tmp_path)
    with pytest.raises(AhShapeError):
        await client.bonus_offers(
            BonusPeriod(date(2026, 10, 26), date(2026, 11, 1), None, ["A", "B"]))
    assert client.health.fails["bonus"] == 1
    await http.aclose()


async def test_403_stops_the_folder_at_once_and_is_logged(respx_mock, tmp_path, caplog):
    caplog.set_level(logging.WARNING, logger="weekmenu.ahclient")
    route = respx_mock.get(SECTION).respond(403, text="<html>blocked</html>")
    client, http = make(tmp_path)
    with pytest.raises(AhApiError) as e:
        await client.bonus_offers(
            BonusPeriod(date(2026, 10, 26), date(2026, 11, 1), None, ["A", "B", "C"]))
    assert e.value.status == 403 and route.call_count == 1
    assert "HTTP 403" in caplog.text and "<html>" not in caplog.text
    assert client.health.fails["bonus"] == 1
    await http.aclose()


async def test_planner_stops_at_a_403_on_a_recipe_search(tmp_path):
    from app.planner import _blocking
    assert _blocking(AhApiError("x", 403))


async def test_shared_health_object(tmp_path):
    h = ApiHealth()
    client, http = make(tmp_path, health=h)
    assert client.health is h
    await http.aclose()


# --- the app version as an option -----------------------------------------------------------


async def test_app_version_goes_into_user_agent_and_client_version(respx_mock, tmp_path):
    route = respx_mock.get(SEARCH).respond(json={"products": []})
    client, http = make(tmp_path, app_version="9.31")
    await client.search_products("ui")
    sent = route.calls[0].request.headers
    assert sent["x-client-version"] == "9.31"
    assert sent["User-Agent"].startswith("Appie/9.31 ")
    await http.aclose()


async def test_default_app_version_is_928(tmp_path):
    client, http = make(tmp_path)
    assert client.app_version == "9.28"
    assert client._headers()["x-client-version"] == "9.28"
    await http.aclose()
