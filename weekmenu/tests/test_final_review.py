"""2.0.0 final review: the choice list fill, the expired login, broken recipes.json."""

from datetime import datetime, timedelta

from app import main
from app.ahclient import AhLoginExpired
from app.tokens import Tokens
from app.web import make_app
from tests.fakes import NEXT, W, make_ctx
from tests.test_catalog import BONUS, Ah

LOOK = datetime(2026, 11, 1, 9, 0)


def small_folder(tmp_path):
    """NEXT is out and AH finds only a few recipes: the list never reaches 60."""
    ah = Ah(tmp_path)
    ah.recipe_hits = {"gehakt": [101, 103], "kipfilet": [102]}
    ctx = make_ctx(tmp_path, ah=ah, now=LOOK, catalog_path=tmp_path / "catalog.json")
    ctx.bonus_cache = {NEXT.start: list(BONUS)}
    ah.store.save(Tokens("secret-access-token", "secret-refresh-token", 9e9))
    return ctx, ah


async def test_a_small_folder_is_filled_once_a_day(tmp_path):
    ctx, ah = small_folder(tmp_path)
    await main.fill_catalog(ctx, LOOK, W)
    first = ah.calls.count("search_recipes")
    assert first > 0 and len(ctx.catalog.entries(W)) == 3
    await main.fill_catalog(ctx, LOOK + timedelta(hours=1), W)
    assert ah.calls.count("search_recipes") == first  # the next hour: no searches
    await main.warm_folder(ctx)  # a restart the same day neither
    assert ah.calls.count("search_recipes") == first
    nxt = LOOK + timedelta(days=1)
    ctx.clock = lambda: nxt
    await main.fill_catalog(ctx, nxt, W)  # the next day it may try again
    assert ah.calls.count("search_recipes") == 2 * first


async def test_an_expired_login_in_the_fill_pushes_once(tmp_path):
    ctx, ah = small_folder(tmp_path)
    ah.raise_on["search_recipes"] = AhLoginExpired("verlopen", 401)
    await main.fill_catalog(ctx, LOOK, W)
    assert ctx.login_expired is True
    pushes = [n for n in ctx.hass.notifications() if n.get("message") == main.LOGIN_EXPIRED]
    assert len(pushes) == 1
    assert W not in ctx.catalog_tried and NEXT.start not in ctx.catalog_tried
    await main.fill_catalog(ctx, LOOK + timedelta(hours=1), W)  # still expired: no new push
    assert [n for n in ctx.hass.notifications()
            if n.get("message") == main.LOGIN_EXPIRED] == pushes


async def test_draft_target_notes_an_expired_login_without_a_push(aiohttp_client, tmp_path):
    ctx, ah = small_folder(tmp_path)
    ah.raise_on["active_order"] = AhLoginExpired("verlopen", 401)
    client = await aiohttp_client(make_app(ctx))
    week = await (await client.get(f"api/week?week={W.isoformat()}")).json()
    assert week["target"] is None
    assert ctx.login_expired is True
    assert ctx.hass.notifications() == []


def test_a_broken_recipes_file_is_set_aside_before_a_save(tmp_path, caplog):
    from app.recipes import OwnRecipe, RecipeStore
    path = tmp_path / "recipes.json"
    path.write_bytes(b'{"schema": 1, "recipes": [{"id": -1, "title": "Oma\'s sto')
    store = RecipeStore(path)
    assert store.all() == []  # shown as empty
    saved = store.save(OwnRecipe(0, "Soep", 2, ["1 ui"]))
    aside = list(tmp_path.glob("recipes.json.broken-*"))
    assert len(aside) == 1
    assert aside[0].read_bytes().startswith(b'{"schema": 1, "recipes": [{"id": -1')
    assert [r.title for r in store.all()] == ["Soep"] and saved.id < 0
    assert "niet te lezen" in caplog.text
    store.save(OwnRecipe(0, "Stoof", 2, ["1 ui"]))  # a readable file now: nothing set aside
    assert len(list(tmp_path.glob("recipes.json.broken-*"))) == 1
    assert [r.title for r in store.all()] == ["Soep", "Stoof"]


def test_a_recipes_file_with_a_wrong_shape_is_set_aside_too(tmp_path):
    from app.recipes import OwnRecipe, RecipeStore
    path = tmp_path / "recipes.json"
    path.write_text('{"schema": 1, "recipes": {"-1": "x"}}', encoding="utf-8")
    RecipeStore(path).save(OwnRecipe(0, "Soep", 2, []))
    assert len(list(tmp_path.glob("recipes.json.broken-*"))) == 1


async def test_an_own_recipe_scores_like_the_same_ah_recipe(tmp_path):
    from app.catalog import choose_view, fill
    from app.recipes import OwnRecipe
    ctx, ah = small_folder(tmp_path)
    await fill(ctx, W)
    # AH's Pasta bolognese (101): 500 g rundergehakt for 2; the own copy is for 4
    own = ctx.recipes.save(OwnRecipe(0, "Bolognese van oma", 4, ["1000 g rundergehakt"]))
    items = {x["id"]: x for x in (await choose_view(ctx, W, None))["items"]}
    assert items[own.id]["savings"] == items[101]["savings"] == 3.0


async def test_a_dish_says_where_its_recipe_can_be_read(aiohttp_client, tmp_path):
    from app.recipes import OwnRecipe
    from tests.test_choose_web import make as choose_ctx
    from tests.test_choose_web import pick
    ctx, ah = choose_ctx(tmp_path)
    typed = ctx.recipes.save(OwnRecipe(0, "Soep van oma", 2, ["500 g uien"], ["Kook."]))
    got = ctx.recipes.save(OwnRecipe(0, "Shakshuka", 2, ["4 eieren"], [],
                                     source="leukerecepten.nl",
                                     url="https://www.leukerecepten.nl/shakshuka"))
    client = await aiohttp_client(make_app(ctx))
    for day, rid in ((W, typed.id), (W + timedelta(days=1), got.id),
                     (W + timedelta(days=2), 101)):
        assert (await pick(client, day, rid)).status == 200
    views = [await (await client.get(f"api/dish/{(W + timedelta(days=i)).isoformat()}")).json()
             for i in range(3)]
    assert [v["recipe"]["source"] for v in views] == ["eigen", "leukerecepten.nl", "ah"]
    assert views[0]["recipe"]["url"] == ""
