"""v0.9: "Klopt niet" choices (overrides.json) and the swap history (history.json)."""

import dataclasses
import json
from datetime import date, datetime

import pytest

from app.ahclient import Product, Recipe
from app.durable import SchemaTooNew
from app.history import History
from app.ingredients import (
    PANTRY,
    PRODUCT,
    SKIP,
    Amount,
    choose_product,
    is_pantry,
    override_key,
)
from app.overrides import OVERRIDES_SCHEMA, Overrides, make_entry
from app.planner import (
    Dish,
    Line,
    Plan,
    _finish,
    _lines,
    _resolver,
    apply_override,
    rescale_dish,
)

AT = datetime(2026, 11, 11, 9, 30)
PANTRY_LIST = ["olie", "zout", "peper"]


def prod(id, title, *, bonus=False, unit="500 g", regular=2.0):
    return Product(id, title, unit, regular, regular, bonus,
                   "25% korting" if bonus else None, True, None)


class FakeClient:
    def __init__(self, results=None):
        self.results = results or {}
        self.queries = []

    async def search_products(self, query, size=20):
        self.queries.append(query)
        return list(self.results.get(query, []))

    async def get_recipe(self, id, servings):
        raise AssertionError("no recipe call expected")


def choice(kind, product=None, text="x"):
    return make_entry(kind, text, product, AT)


# --- the key ----------------------------------------------------------------------------


@pytest.mark.parametrize("name, key", [
    ("kipdijfilet", "kipdijfilet"),
    ("2-3 el Olijfolie", "olijfolie"),
    ("1 1/2 tl gemalen komijn", "gemalen komijn"),
    ("400 g crème fraîche (light)", "creme fraiche"),
    ("1 rode ui", "rode ui"),
    ("  Rode  Ui ", "rode ui"),
])
def test_override_key_is_the_name_without_amount(name, key):
    assert override_key(name) == key


# --- before the heuristics --------------------------------------------------------------


def test_pantry_choice_goes_before_the_pantry_list():
    ov = {"kokosmelk": choice(PANTRY), "rijst": choice(SKIP),
          "olijfolie": choice(PRODUCT, prod(5, "Bertolli olijfolie"))}
    assert is_pantry("kokosmelk", PANTRY_LIST, ov)
    assert is_pantry("rijst", PANTRY_LIST, ov)  # never bought
    assert not is_pantry("olijfolie", PANTRY_LIST, ov)  # "Ander product": bought after all
    assert is_pantry("olijfolie", PANTRY_LIST)
    assert not is_pantry("kokosmelk", PANTRY_LIST)


async def test_product_choice_goes_before_search_and_bonus():
    chosen = prod(7, "AH Rode uien")
    client = FakeClient({"rode ui": [prod(1, "AH Witte uien")]})
    ov = {"rode ui": choice(PRODUCT, chosen)}
    p = await choose_product(client, Amount(1, "stuks", "rode ui"),
                             [prod(9, "AH Rode ui", bonus=True)], {}, ov)
    assert p == chosen and client.queries == []
    # the bonus version of the same product, with this week's price
    fresh = dataclasses.replace(chosen, now=1.0, is_bonus=True, mechanism="1+1 gratis")
    p = await choose_product(client, Amount(1, "stuks", "rode ui"), [fresh], {}, ov)
    assert p is fresh
    assert await choose_product(client, Amount(1, "stuks", "rode ui"), [],
                                overrides={"rode ui": choice(SKIP)}) is None


async def test_resolver_puts_skip_pantry_and_product_choices_in_their_place():
    r = Recipe(1, "R", "", 30, None, None, ["hoofdgerecht"], [], 2,
               [("1 rode ui", "rode ui"), ("200 ml kokosmelk", "kokosmelk"),
                ("1 tl komijn", "komijn"), ("2 el olie", "olie")], [], None)
    ov = {"rode ui": choice(PRODUCT, prod(7, "AH Rode uien", unit="1 kg")),
          "kokosmelk": choice(PANTRY), "komijn": choice(SKIP)}
    client = FakeClient()
    lines, pantry, not_found, skipped = await _lines(
        client, r, _resolver(client, [], PANTRY_LIST, {}, ov))
    assert [ln.product.id for ln in lines] == [7]
    assert pantry == ["200 ml kokosmelk", "2 el olie"]
    assert skipped == ["1 tl komijn"] and not_found == [] and client.queries == []


# --- the file -----------------------------------------------------------------------------


def test_overrides_file_has_a_schema_and_survives_a_reload(tmp_path):
    store = Overrides(tmp_path / "overrides.json")
    assert store.load() == {}
    store.put("rode ui", choice(PRODUCT, prod(7, "AH Rode uien"), "1 rode ui"))
    store.put("komijn", choice(SKIP, text="1 tl komijn"))
    raw = json.loads((tmp_path / "overrides.json").read_text(encoding="utf-8"))
    assert raw["schema"] == OVERRIDES_SCHEMA
    again = Overrides(tmp_path / "overrides.json").load()
    assert again["rode ui"]["product"]["title"] == "AH Rode uien"
    assert again["komijn"]["kind"] == SKIP
    listing = store.listing()
    assert {x["key"] for x in listing} == {"rode ui", "komijn"}
    assert next(x for x in listing if x["key"] == "rode ui")["product"] == "AH Rode uien"
    assert store.remove("komijn") is True and store.remove("komijn") is False
    assert set(store.load()) == {"rode ui"}


def test_overrides_file_from_a_newer_add_on_is_never_written(tmp_path):
    path = tmp_path / "overrides.json"
    path.write_text(json.dumps({"schema": 99, "items": {}}), encoding="utf-8")
    store = Overrides(path)
    assert store.too_new()
    with pytest.raises(SchemaTooNew):
        store.put("komijn", choice(SKIP))
    assert json.loads(path.read_text(encoding="utf-8")) == {"schema": 99, "items": {}}


def test_broken_overrides_file_counts_as_no_choices_and_is_kept_as_bak(tmp_path):
    path = tmp_path / "overrides.json"
    path.write_text("{half", encoding="utf-8")
    store = Overrides(path)
    assert store.load() == {}
    store.put("komijn", choice(SKIP))
    assert (tmp_path / "overrides.json.bak").read_text(encoding="utf-8") == "{half"
    assert set(store.load()) == {"komijn"}


def test_invalid_entries_are_ignored(tmp_path):
    path = tmp_path / "overrides.json"
    path.write_text(json.dumps({"schema": 1, "items": {
        "a": {"kind": "weg"}, "b": {"kind": "product"}, "c": {"kind": "skip"},
        "d": {"kind": "product", "product": {"id": 1}}}}), encoding="utf-8")
    assert set(Overrides(path).load()) == {"c"}


# --- on the menu as it is -------------------------------------------------------------------


def _menu():
    ui = prod(1, "AH Witte uien", unit="1 kg")
    kip = prod(2, "AH Kipfilet")
    r1 = Recipe(11, "Een", "", 30, None, None, ["hoofdgerecht"], [], 2,
                [("1 rode ui", "rode ui"), ("300 g kipfilet", "kipfilet"),
                 ("1 tl komijn", "komijn")], [], None)
    r2 = Recipe(12, "Twee", "", 30, None, None, ["hoofdgerecht"], [], 2,
                [("2 rode uien", "rode ui")], [], None)
    d1 = Dish(date(2026, 11, 11), r1, "x", False,
              [Line(ui, 1, "1 rode ui", 0.1), Line(kip, 1, "300 g kipfilet", 0.6)], 0.0,
              not_found=["1 tl komijn"])
    d2 = Dish(date(2026, 11, 12), r2, "x", False, [Line(ui, 1, "2 rode uien", 0.2)], 0.0)
    return _finish([d1, d2], 0, PANTRY_LIST)


def test_apply_override_changes_every_dish_with_the_ingredient_only():
    plan = _menu()
    rood = prod(7, "AH Rode uien", unit="3 stuks")
    new = apply_override(plan, "rode ui", {"rode ui": choice(PRODUCT, rood)})
    assert new.basket == {7: 1, 2: 1}  # 1 + 2 red onions from one pack of 3
    assert [ln.product.id for ln in new.dishes[0].lines] == [7, 2]  # recipe order
    assert new.dishes[0].not_found == ["1 tl komijn"]

    skip = apply_override(new, "komijn", {"komijn": choice(SKIP)})
    assert skip.dishes[0].not_found == [] and skip.dishes[0].skipped == ["1 tl komijn"]
    assert skip.not_found == []

    home = apply_override(plan, "kipfilet", {"kipfilet": choice(PANTRY)})
    assert 2 not in home.basket and home.dishes[0].pantry == ["300 g kipfilet"]

    assert apply_override(plan, "spinazie", {"spinazie": choice(SKIP)}) is plan


def test_skipped_survives_the_log():
    plan = apply_override(_menu(), "komijn", {"komijn": choice(SKIP)})
    again = Plan.from_dict(json.loads(json.dumps(plan.to_dict())))
    assert again.dishes[0].skipped == ["1 tl komijn"]
    old = plan.to_dict()
    for d in old["dishes"]:
        del d["skipped"]  # a log from before 0.9
    assert Plan.from_dict(old).dishes[0].skipped == []


async def test_rescale_keeps_the_choices():
    plan = apply_override(_menu(), "komijn", {"komijn": choice(SKIP)})
    ov = {"komijn": choice(SKIP)}

    class Scaling(FakeClient):
        async def get_recipe(self, id, servings):
            assert id == 11
            return dataclasses.replace(plan.dishes[0].recipe, servings=servings, ingredients=[
                ("2 rode uien", "rode ui"), ("600 g kipfilet", "kipfilet"),
                ("2 tl komijn", "komijn")])

    client = Scaling()
    new = await rescale_dish(client, plan, date(2026, 11, 11), 4, ov)
    assert new.dishes[0].skipped == ["2 tl komijn"] and new.dishes[0].not_found == []
    assert client.queries == []


# --- history -----------------------------------------------------------------------------------


def test_history_records_swaps_and_removals_per_week(tmp_path):
    h = History(tmp_path / "history.json")
    w = date(2026, 11, 11)
    h.record(w, date(2026, 11, 12), 101, "Pasta", "swap", AT)
    h.record(w, date(2026, 11, 13), 102, "Curry", "remove", AT)
    raw = json.loads((tmp_path / "history.json").read_text(encoding="utf-8"))
    assert raw["schema"] == 1
    assert [e["action"] for e in raw["weeks"]["2026-11-11"]] == ["swap", "remove"]
    assert raw["weeks"]["2026-11-11"][0] == {"date": "2026-11-12", "recipe_id": 101,
                                             "title": "Pasta", "action": "swap",
                                             "at": "2026-11-11T09:30"}
    with pytest.raises(ValueError):
        h.record(w, w, 1, "x", "why", AT)


def test_history_from_a_newer_add_on_is_never_written(tmp_path):
    path = tmp_path / "history.json"
    path.write_text(json.dumps({"schema": 5, "weeks": {}}), encoding="utf-8")
    with pytest.raises(SchemaTooNew):
        History(path).record(date(2026, 11, 11), date(2026, 11, 11), 1, "x", "swap", AT)
    assert History(path).too_new()
