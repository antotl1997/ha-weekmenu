"""v1.1: the choice list. AH recipes scored per folder period, own recipes mixed in."""

import json
from datetime import date, datetime

from app import planner
from app.ahclient import Product, Recipe
from app.catalog import KEEP, Catalog, Entry, choose_view, fill, search_ah
from app.ledger import WeekLog
from app.recipes import OwnRecipe
from app.taste import DOWN, NEVER
from tests.fakes import CURRENT, NEXT, W, FakeAh, dish, make_ctx, make_plan

GEHAKT = Product(11, "AH Rundergehakt", "500 g", 6.0, 6.0, True, "1+1 gratis", True, "Vlees")
KIP = Product(12, "AH Kipfilet", "500 g", 4.0, 4.0, True, "1+1 gratis", True, "Kip")
BONUS = [GEHAKT, KIP]
TUESDAY, SATURDAY = date(2026, 11, 10), date(2026, 11, 7)


def rec(rid, title, ing, cook=30):
    return Recipe(rid, title, f"https://example.invalid/r/{rid}", cook, None, None,
                  ["hoofdgerecht"], [], 2, [(f"500 g {ing}", ing)], ["Kook het."],
                  f"https://example.invalid/{rid}.jpg")


RECIPES = {
    101: rec(101, "Pasta bolognese", "rundergehakt", cook=60),
    102: rec(102, "Kip uit de oven", "kipfilet", cook=20),
    103: rec(103, "Gehaktballen met jus", "rundergehakt"),
    104: rec(104, "Stamppot andijvie", "andijvie"),
    105: rec(105, "Lasagne al forno", "rundergehakt", cook=50),
}


class Ah(FakeAh):
    async def get_recipe(self, id, servings):
        self._maybe_raise("get_recipe")
        return RECIPES[id]


def ctx_with_folder(tmp_path, **kw):
    ah = Ah(tmp_path)
    terms = planner.search_terms(BONUS)
    assert terms == ["gehakt", "kipfilet"]  # the best saving first
    ah.recipe_hits = {"gehakt": [101, 103, 105], "kipfilet": [102], "stamppot": [104]}
    ctx = make_ctx(tmp_path, ah=ah, catalog_path=tmp_path / "catalog.json", **kw)
    ctx.bonus_cache = {NEXT.start: list(BONUS)}
    return ctx, ah


def by_id(view):
    return {x["id"]: x for x in view["items"]}


async def test_fill_scores_and_keeps_per_period(tmp_path):
    ctx, ah = ctx_with_folder(tmp_path)
    assert await fill(ctx, W) == 4
    entries = {e.id: e for e in ctx.catalog.entries(W)}
    assert set(entries) == {101, 102, 103, 105}
    assert entries[101].score == 3.0  # half of the 1+1 deal on the gehakt
    assert entries[101].cuisine == "pasta en Italiaans" and entries[101].cook == 60
    assert entries[102].score == 2.0
    calls = ah.calls.count("get_recipe")
    assert await fill(ctx, W) == 0
    assert ah.calls.count("get_recipe") == calls  # nothing fetched twice
    assert ctx.catalog.has(W)
    data = json.loads((tmp_path / "catalog.json").read_text(encoding="utf-8"))
    assert data["schema"] == 1
    again = Catalog(tmp_path / "catalog.json")  # after a restart
    assert again.entries(W) == ctx.catalog.entries(W)
    assert again.entries(NEXT.start) == ctx.catalog.entries(W)  # one period, any day of it


async def test_fill_respects_budget_and_never(tmp_path):
    ctx, ah = ctx_with_folder(tmp_path)
    ctx.taste.put(103, NEVER, "Gehaktballen met jus", datetime(2026, 11, 1, 8, 0))
    assert await fill(ctx, W, budget=2) == 2
    assert ah.calls.count("get_recipe") == 2
    assert 103 not in {e.id for e in ctx.catalog.entries(W)}
    assert await fill(ctx, W) == 1  # the rest, still without the "nooit meer" one
    assert {e.id for e in ctx.catalog.entries(W)} == {101, 102, 105}


async def test_choose_view_orders_by_savings_and_merges_own(tmp_path):
    ctx, ah = ctx_with_folder(tmp_path)
    await fill(ctx, W)
    own = ctx.recipes.save(OwnRecipe(0, "Oma's gehaktschotel", 4, ["1 kg rundergehakt"]))
    ctx.taste.put(105, DOWN, "Lasagne al forno", datetime(2026, 11, 1, 8, 0))
    calls = list(ah.calls)
    view = await choose_view(ctx, W, None)
    assert ah.calls == calls  # the list itself never asks AH
    assert view["catalog_ready"] is True
    assert "pasta en Italiaans" in view["kinds"] and "Hollandse pot" in view["kinds"]
    items = view["items"]
    assert items[0]["id"] == own.id and items[0]["source"] == "eigen"
    assert items[0]["savings"] == 6.0  # two packs of gehakt for 4 persons
    savings = [x["savings"] for x in items]
    assert savings == sorted(savings, reverse=True)
    assert [x["id"] for x in items[1:3]] == [103, 101]  # equal savings: by title
    assert by_id(view)[105]["savings"] == round(3.0 * planner.DOWN_FACTOR, 2)
    assert {x["source"] for x in items[1:]} == {"ah"}
    assert set(items[0]) == {"id", "title", "image", "cook", "cuisine", "savings",
                             "source", "long", "eaten_weeks_ago"}


async def test_choose_view_filters_and_marks(tmp_path):
    ctx, _ = ctx_with_folder(tmp_path)
    await fill(ctx, W)
    ctx.recipes.save(OwnRecipe(0, "Pasta van de buren", 2, ["250 g spaghetti"],
                               source="Leukerecepten", kind="pasta en Italiaans"))
    week_before = date(2026, 10, 28)
    plan = make_plan([dish(week_before, 102, [1])])
    ctx.ledger.save(WeekLog(week=week_before, plan=plan.to_dict()))

    view = await choose_view(ctx, W, TUESDAY, q="PASTA")
    assert [x["title"] for x in view["items"]] == ["Pasta bolognese", "Pasta van de buren"]
    assert view["items"][1]["source"] == "Leukerecepten"

    kinds = (await choose_view(ctx, W, TUESDAY, kind="pasta en Italiaans"))["items"]
    assert {x["title"] for x in kinds} == {"Pasta bolognese", "Lasagne al forno",
                                          "Pasta van de buren"}

    tuesday = by_id(await choose_view(ctx, W, TUESDAY))
    assert tuesday[101]["long"] is True  # 60 minutes on a weekday
    assert tuesday[105]["long"] is True and tuesday[103]["long"] is False
    assert by_id(await choose_view(ctx, W, SATURDAY))[101]["long"] is False
    assert by_id(await choose_view(ctx, W, None))[101]["long"] is False
    assert tuesday[102]["eaten_weeks_ago"] == 1
    assert tuesday[101]["eaten_weeks_ago"] is None


async def test_search_ah_adds_to_the_catalog(tmp_path):
    ctx, ah = ctx_with_folder(tmp_path)
    assert (await choose_view(ctx, W, None, q="stamppot"))["items"] == []
    found = await search_ah(ctx, W, "stamppot")
    assert [e.id for e in found] == [104]
    assert found[0].title == "Stamppot andijvie"
    assert [x["id"] for x in (await choose_view(ctx, W, None, q="stamppot"))["items"]] == [104]
    calls = ah.calls.count("get_recipe")
    assert [e.id for e in await search_ah(ctx, W, "stamppot")] == [104]  # known: no fetch
    assert ah.calls.count("get_recipe") == calls


async def test_empty_catalog_still_lists_own_recipes(tmp_path):
    ah = Ah(tmp_path)  # no login, nothing cached
    ctx = make_ctx(tmp_path, ah=ah)
    own = ctx.recipes.save(OwnRecipe(0, "Hachee", 4, ["1 kg rundergehakt"],
                                     kind="Hollandse pot"))
    view = await choose_view(ctx, W, TUESDAY)
    assert view["catalog_ready"] is False
    assert [(x["id"], x["savings"], x["cuisine"]) for x in view["items"]] == \
        [(own.id, 0, "Hollandse pot")]
    assert ah.calls == []


def test_catalog_dedupes_and_keeps_the_newest_periods(tmp_path):
    cat = Catalog(tmp_path / "catalog.json")
    e = Entry(1, "Een", None, 30, None, None, None, 1.0, False)
    cat.add(NEXT.start, [e, e])
    cat.add(NEXT.start, [Entry(1, "Een", None, 30, None, None, None, 2.0, False)])
    assert len(cat.entries(NEXT.start)) == 1
    starts = [date(2026, 11, 2 + 7 * i) for i in range(KEEP + 1)]
    for s in starts:
        cat.add(s, [e])
    assert not cat.has(starts[0]) and all(cat.has(s) for s in starts[1:])
    assert Catalog(None).entries(W) == []  # no file: memory only


async def test_own_recipe_in_kilos_or_pieces_scores_against_the_folder(tmp_path):
    ctx, _ = ctx_with_folder(tmp_path)
    kilo = ctx.recipes.save(OwnRecipe(0, "Gehaktbrood", 4, ["1 kg rundergehakt"]))
    stuks = ctx.recipes.save(OwnRecipe(0, "Burgers", 4, ["2 stuks rundergehakt"]))
    got = by_id(await choose_view(ctx, W, None))
    assert got[kilo.id]["savings"] > 0 and got[stuks.id]["savings"] > 0


async def test_fill_with_the_current_folder_shows_in_the_menu_week(tmp_path):
    """Sunday, next week's folder not out: the list is filled and scored with the current
    folder, and the menu week's list shows it."""
    ah = Ah(tmp_path, periods=(CURRENT,))
    ah.recipe_hits = {"gehakt": [101, 103], "kipfilet": [102], "stamppot": [104]}
    ctx = make_ctx(tmp_path, ah=ah, catalog_path=tmp_path / "catalog.json")
    ctx.bonus_cache = {CURRENT.start: list(BONUS)}
    own = ctx.recipes.save(OwnRecipe(0, "Gehaktbrood", 4, ["1 kg rundergehakt"]))
    assert await fill(ctx, W) == 3
    assert ctx.catalog.periods() == [CURRENT.start]
    assert [e.id for e in await search_ah(ctx, W, "stamppot")] == [104]
    calls = list(ah.calls)
    view = await choose_view(ctx, W, None)
    assert ah.calls == calls
    assert view["catalog_ready"] is True
    got = by_id(view)
    assert set(got) == {101, 102, 103, 104, own.id}
    assert got[own.id]["savings"] == 6.0  # scored with the same (current) folder


async def test_an_old_period_does_not_stand_in(tmp_path):
    ctx, _ = ctx_with_folder(tmp_path)
    ctx.bonus_cache = {}
    old = date(2026, 10, 12)  # three weeks before the menu week
    ctx.catalog.add(old, [Entry(1, "Oud", None, 30, None, None, None, 1.0, False)])
    view = await choose_view(ctx, W, None)
    assert view["items"] == [] and view["catalog_ready"] is False


async def test_entries_get_the_cuisine_of_their_search_term(tmp_path):
    ctx, _ = ctx_with_folder(tmp_path)
    plain = rec(106, "Kip uit de pan", "kipfilet")  # no cuisine from title or lines
    RECIPES[106] = plain
    try:
        assert await planner.score_recipe(plain, BONUS, []) == (2.0, None, False)
        ctx.client.recipe_hits = {"pasta": [106]}
        await search_ah(ctx, W, "pasta")
        assert {e.id: e.cuisine for e in ctx.catalog.entries(W)}[106] == "pasta en Italiaans"
    finally:
        del RECIPES[106]
