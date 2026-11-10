"""v0.7: the local contract check of tools/record_fixtures.py, offline."""
import json
import time
from pathlib import Path

import httpx
import pytest

from app.tokens import Tokens, TokenStore
from tools import record_fixtures as rf
from tools.record_fixtures import check, check_one, compare_shapes

FIX = Path(__file__).parent / "fixtures"
BASE = "https://api.ah.nl"


def test_same_shape_has_no_differences():
    a = {"periods": [{"start": "2026-11-04", "n": 1, "tabs": [{"x": True}]}]}
    b = {"periods": [{"start": "2027-01-06", "n": 2.5, "tabs": [{"x": False}]},
                     {"anders": 1}]}
    assert compare_shapes(a, b) == []


def test_missing_new_and_retyped_keys_are_listed_with_their_path():
    a = {"items": [{"quantity": 2, "productDetails": {"product": {"webshopId": 1}}}],
         "id": "x"}
    b = {"entries": [], "id": 5, "items": [{"quantity": "2",
                                            "productDetails": {"product": {}}}]}
    assert sorted(compare_shapes(a, b)) == [
        "$.entries: nieuw",
        "$.id: type str -> number",
        "$.items[0].productDetails.product.webshopId: ontbreekt",
        "$.items[0].quantity: type number -> str",
    ]


def test_null_and_empty_lists_match_anything():
    assert compare_shapes({"a": None, "b": []}, {"a": {"x": 1}, "b": [{"y": 2}]}) == []
    assert compare_shapes({"a": 1}, {"a": None}) == []


def test_list_against_object_is_a_type_change():
    assert compare_shapes({"a": []}, {"a": {}}) == ["$.a: type list -> object"]


def test_differences_never_carry_a_value():
    out = compare_shapes({"t": "geheim-1"}, {"t": 12345, "n": "geheim-2"})
    assert out and not any("geheim" in d or "12345" in d for d in out)


def test_check_one_names_the_fixture_and_a_status_change():
    assert check_one("x.json", 200, {"a": 1}, 200, {"a": 1}) == []
    assert check_one("x.json", 200, {}, 200, {"a": 1}) == ["x.json: $.a: ontbreekt"]
    assert check_one("x.json", 403, "<html>", 200, {"a": 1}) == [
        "x.json: HTTP 200 -> 403 (vorm niet vergeleken)"]


def fx(name):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


def serve_fixtures(respx_mock, list_body=None):
    respx_mock.get(BASE + "/mobile-services/bonuspage/v3/metadata").respond(
        json=fx("bonus-metadata__v3.json"))
    respx_mock.get(BASE + "/mobile-services/bonuspage/v2/section").respond(
        json=fx("bonus-section__p0-vlees.json"))
    recipe = sorted(FIX.glob("graphql-recipe__*-s2.json"))[0]

    def graphql(request):
        q = json.loads(request.content)["query"]
        assert "mutation" not in q
        name = "graphql-recipesearch__kip.json" if "recipeSearch" in q else recipe.name
        return httpx.Response(200, json=fx(name))

    respx_mock.post(BASE + "/graphql").mock(side_effect=graphql)
    respx_mock.get(BASE + "/mobile-services/product/search/v2").respond(
        json=fx("product-search__biologische-citroen.json"))
    respx_mock.get(BASE + "/mobile-services/shoppinglist/v2/items").respond(
        json=list_body or fx("shoppinglist__items.json")["body"])
    order = fx("order-active__default.json")
    respx_mock.get(BASE + "/mobile-services/order/v1/summaries/active").respond(
        order["status"], json=order["body"])


@pytest.fixture
def tokens(tmp_path, monkeypatch):
    monkeypatch.setattr(rf, "DELAY", 0)
    path = tmp_path / "tokens.json"
    TokenStore(path).save(Tokens("a", "r", time.time() + 3600))
    return path


async def test_check_against_the_fixtures_themselves_finds_nothing(respx_mock, tokens, capsys):
    serve_fixtures(respx_mock)
    assert await check(tokens) == 0
    out = capsys.readouterr().out
    assert "Geen verschillen" in out
    assert all(r.request.method in ("GET", "POST") for r in respx_mock.calls)


async def test_check_reports_a_renamed_field(respx_mock, tokens, capsys):
    body = fx("shoppinglist__items.json")["body"]
    body["entries"] = body.pop("items")
    serve_fixtures(respx_mock, body)
    assert await check(tokens) == 1
    out = capsys.readouterr().out
    assert "shoppinglist__items.json: $.items: ontbreekt" in out
    assert "shoppinglist__items.json: $.entries: nieuw" in out
