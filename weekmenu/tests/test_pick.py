"""v1.1: the planner builds a dish from a recipe the user picked."""

from datetime import timedelta

import pytest

from app import planner
from app.ahclient import AhApiError, Product
from app.planner import dish_for, empty_plan, pick_dish, rebuild_plan, rescale_dish
from app.recipes import LocalFirst, OwnRecipe, RecipeStore
from tests.fakes import W, FakeAh

PANTRY = ["olie", "zout", "peper"]


def ui(pid: int) -> Product:
    """A bonus onion: FakeAh's recipes all need "1 stuk ui"."""
    return Product(pid, "AH Ui", "1 kg", 2.0, 1.0, True, "2e halve prijs", True, "Groente")


def pasta(pid: int) -> Product:
    return Product(pid, "AH Pasta penne", "500 g", 2.0, 1.0, True, "2e halve prijs", True,
                   None)


def make_client(tmp_path):
    ah = FakeAh(tmp_path)
    store = RecipeStore(tmp_path / "recipes.json")
    return ah, store, LocalFirst(ah, store)


def own_pasta(store: RecipeStore) -> OwnRecipe:
    return store.save(OwnRecipe(0, "Pasta van oma", 4, ["400 g pasta"], ["Kook."], 20, None,
                                None, None, None, "2026-11-12T10:00"))


async def test_pick_sets_a_dish_on_an_empty_evening(tmp_path):
    _, _, client = make_client(tmp_path)
    plan = await pick_dish(client, empty_plan(PANTRY), W, 7, [ui(10)], 2, None)
    assert [d.date for d in plan.dishes] == [W]
    d = plan.dishes[0]
    assert d.recipe.id == 7 and d.persons == 2
    assert plan.basket == {10: 1}
    assert plan.pantry == PANTRY and d.cook == 30


async def test_pick_replaces_and_keeps_the_rest(tmp_path):
    _, _, client = make_client(tmp_path)
    plan = await pick_dish(client, empty_plan(PANTRY), W, 7, [ui(10)], 2, None)
    plan = await pick_dish(client, plan, W + timedelta(1), 8, [ui(10)], 3, None)
    before = plan.dishes[0]
    new = await pick_dish(client, plan, W + timedelta(1), 9, [ui(10)], 3, None)
    assert [d.date for d in new.dishes] == [W, W + timedelta(1)]
    assert new.dishes[0].recipe == before.recipe and new.dishes[0].lines == before.lines
    assert new.dishes[1].recipe.id == 9 and new.dishes[1].persons == 3


async def test_pick_clears_the_removed_day_and_its_leftover(tmp_path):
    _, _, client = make_client(tmp_path)
    plan = await pick_dish(client, empty_plan(PANTRY), W, 7, [ui(10)], 2, W + timedelta(1))
    plan = planner.remove_dish(plan, W)
    assert plan.removed_days == [W, W + timedelta(1)]
    plan = await pick_dish(client, plan, W, 8, [ui(10)], 2, W + timedelta(1))
    assert plan.removed_days == []
    assert plan.dishes[0].leftover_date == W + timedelta(1)


async def test_pick_an_own_recipe_scales_without_ah(tmp_path):
    ah, store, client = make_client(tmp_path)
    r = own_pasta(store)
    plan = await pick_dish(client, empty_plan(PANTRY), W, r.id, [pasta(30)], 2, None)
    assert "get_recipe" not in ah.calls
    d = plan.dishes[0]
    assert d.recipe.ingredients == [("200 g pasta", "")]
    assert d.persons == 2 and d.recipe.id == r.id
    assert d.cuisine == "pasta en Italiaans" and d.is_fish is False and d.cook == 20
    assert plan.basket == {30: 1}


async def test_an_own_recipe_without_a_cuisine_is_eigen(tmp_path):
    _, store, client = make_client(tmp_path)
    r = store.save(OwnRecipe(0, "Recept van oma", 2, ["2 stuks xyzzy"], [], None, None,
                             None, None, None, ""))
    d = await dish_for(client, r.id, W, 2, [], PANTRY)
    assert d.cuisine == "eigen" and d.cook is None


async def test_dish_for_takes_times_from_the_recipe(tmp_path):
    _, _, client = make_client(tmp_path)
    d = await dish_for(client, 7, W, 2, [ui(10)], PANTRY, leftover_date=W + timedelta(1))
    assert (d.cook, d.oven, d.wait) == (30, None, None)
    assert d.leftover_date == W + timedelta(1) and d.date == W


async def test_rebuild_keeps_recipes_and_takes_new_bonus(tmp_path):
    _, _, client = make_client(tmp_path)
    plan = await pick_dish(client, empty_plan(PANTRY), W, 7, [ui(10)], 2, None)
    plan = await pick_dish(client, plan, W + timedelta(1), 8, [ui(10)], 4, W + timedelta(2))
    assert set(plan.basket) == {10}
    new = await rebuild_plan(client, plan, [ui(20)])
    assert [(d.recipe.id, d.date, d.persons, d.leftover_date) for d in new.dishes] == \
        [(d.recipe.id, d.date, d.persons, d.leftover_date) for d in plan.dishes]
    assert set(new.basket) == {20} and set(new.products) == {20}
    assert new.pantry == plan.pantry


async def test_deleted_own_recipe_keeps_the_menu_and_rescales_from_the_snapshot(tmp_path):
    ah, store, client = make_client(tmp_path)
    r = own_pasta(store)
    plan = await pick_dish(client, empty_plan(PANTRY), W, r.id, [pasta(30)], 2, None)
    plan = await pick_dish(client, plan, W + timedelta(1), 7, [ui(10)], 2, None)
    assert store.remove(r.id)
    new = await rebuild_plan(client, plan, [pasta(31), ui(20)])
    own_dish = next(d for d in new.dishes if d.date == W)
    assert own_dish.recipe == next(d for d in plan.dishes if d.date == W).recipe
    assert [ln.product.id for ln in own_dish.lines] == [31]  # the new folder
    assert next(d for d in new.dishes if d.date != W).lines[0].product.id == 20
    calls = ah.calls.count("get_recipe")
    bigger = await rescale_dish(client, new, W, 4)
    d = next(d for d in bigger.dishes if d.date == W)
    assert d.recipe.ingredients == [("400 g pasta", "")] and d.persons == 4
    assert d.recipe.servings == 4 and [ln.product.id for ln in d.lines] == [31]
    assert ah.calls.count("get_recipe") == calls


async def test_edited_own_recipe_does_not_change_the_dish(tmp_path):
    _, store, client = make_client(tmp_path)
    r = own_pasta(store)
    plan = await pick_dish(client, empty_plan(PANTRY), W, r.id, [pasta(30)], 2, None)
    store.save(OwnRecipe(r.id, "Rijst van oma", 4, ["1 kg rijst"], ["Kook."], 20, None,
                         None, None, None, "2026-11-12T11:00"))
    new = await rebuild_plan(client, plan, [pasta(30)])
    assert new.dishes[0].recipe == plan.dishes[0].recipe
    bigger = await rescale_dish(client, new, W, 3)
    assert bigger.dishes[0].recipe.title == "Pasta van oma"
    assert bigger.dishes[0].recipe.ingredients == [("300 g pasta", "")]


async def test_rebuild_keeps_an_ah_dish_whose_recipe_is_gone(tmp_path):
    ah, _, client = make_client(tmp_path)
    plan = await pick_dish(client, empty_plan(PANTRY), W, 7, [ui(10)], 2, None)
    ah.raise_on["get_recipe"] = AhApiError("weg", 404)
    new = await rebuild_plan(client, plan, [ui(20)])
    assert new.dishes == plan.dishes


async def test_rebuild_passes_other_ah_errors_on(tmp_path):
    ah, _, client = make_client(tmp_path)
    plan = await pick_dish(client, empty_plan(PANTRY), W, 7, [ui(10)], 2, None)
    ah.raise_on["get_recipe"] = AhApiError("boom", 500)
    with pytest.raises(AhApiError):
        await rebuild_plan(client, plan, [ui(20)])


def test_empty_plan_has_no_dishes():
    p = empty_plan(PANTRY)
    assert p.dishes == [] and p.basket == {} and p.savings == 0 and p.short_by == 0
    assert p.pantry == PANTRY and p.removed_days == []
