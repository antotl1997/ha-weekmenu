"""Own recipes: the store, scaling and the local-first source."""

import json

import pytest

from app.ahclient import AhApiError
from app.durable import SchemaTooNew
from app.recipes import LocalFirst, OwnRecipe, RecipeStore, scale_text, to_recipe
from tests.fakes import FakeAh, recipe


def own(title="Pasta", servings=4, ingredients=("400 g pasta",)):
    return OwnRecipe(0, title, servings, list(ingredients), ["Kook."], 20, "Italiaans",
                     None, None, None, "2026-11-12T10:00")


def test_save_assigns_negative_ids_and_round_trips(tmp_path):
    path = tmp_path / "recipes.json"
    store = RecipeStore(path)
    a, b = store.save(own("A")), store.save(own("B"))
    assert (a.id, b.id) == (-1, -2)
    assert RecipeStore(path).all() == [a, b]
    assert json.loads(path.read_text(encoding="utf-8"))["schema"] == 1


def test_remove_and_unknown(tmp_path):
    store = RecipeStore(tmp_path / "recipes.json")
    store.save(own())
    assert store.remove(-1) is True
    assert store.get(-1) is None
    assert store.remove(-1) is False


@pytest.mark.parametrize("text,factor,want", [
    ("500 g rundergehakt", 2, "1000 g rundergehakt"),
    ("1/2 ui", 2, "1 ui"),
    ("1,5 el olie", 2, "3 el olie"),
    ("2-3 teentjes knoflook", 2, "4-6 teentjes knoflook"),
    ("zout", 2, "zout"),
    ("peper naar smaak", 0.5, "peper naar smaak"),
    ("1 ui", 0.5, "0,5 ui"),
])
def test_scale_text(text, factor, want):
    assert scale_text(text, factor) == want


def test_to_recipe_scales_to_persons():
    r = to_recipe(own(), 2)
    assert r.ingredients[0] == ("200 g pasta", "")
    assert r.servings == 2 and r.courses == ["hoofdgerecht"] and r.cuisines == ["Italiaans"]
    assert r.url == ""


async def test_local_first_dispatches_on_sign(tmp_path):
    store = RecipeStore(tmp_path / "recipes.json")
    store.save(own("Eigen"))
    ah = FakeAh(tmp_path)
    lf = LocalFirst(ah, store)
    assert (await lf.get_recipe(-1, 2)).title == "Eigen"
    assert await lf.get_recipe(5, 2) == recipe(5)
    with pytest.raises(AhApiError) as e:
        await lf.get_recipe(-9, 2)
    assert e.value.status == 404
    assert lf.search_products == ah.search_products


def test_broken_or_newer_file_loads_empty(tmp_path):
    path = tmp_path / "recipes.json"
    store = RecipeStore(path)
    assert store.all() == []
    path.write_text("{niet json", encoding="utf-8")
    assert store.all() == []
    path.write_text(json.dumps({"schema": 99, "recipes": [{"id": -1, "title": "x",
                                                            "servings": 2}]}),
                    encoding="utf-8")
    assert store.all() == [] and store.too_new()
    with pytest.raises(SchemaTooNew):
        store.save(own())
