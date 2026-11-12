"""v1.1: the week filled as a draft, evening by evening, and confirmed once (web API)."""

import asyncio
import dataclasses
from datetime import timedelta

import pytest

from app.ahclient import AhApiError, Product, Recipe
from app.catalog import Entry
from app.ledger import WeekLog
from app.planner import Plan
from app.recipes import OwnRecipe
from app.tokens import Tokens
from app.web import make_app
from tests.fakes import NEXT, SUNDAY, W, FakeAh, FakeHass, make_ctx

DO, VR = W + timedelta(days=1), W + timedelta(days=2)

GEHAKT = Product(11, "AH Rundergehakt", "500 g", 6.0, 6.0, True, "1+1 gratis", True, "Vlees")
KIP = Product(12, "AH Kipfilet", "500 g", 4.0, 4.0, True, "1+1 gratis", True, "Kip")
UI = Product(13, "AH Uien", "1 kg", 2.0, 1.0, True, "2e halve prijs", True, "Groente")
BONUS = [GEHAKT, KIP, UI]


def rec(rid, title, ing, servings=2):
    return Recipe(rid, title, f"https://example.invalid/r/{rid}", 30, None, None,
                  ["hoofdgerecht"], [], servings, [(f"500 g {ing}", ing)], ["Kook het."],
                  f"https://example.invalid/{rid}.jpg")


RECIPES = {
    101: rec(101, "Pasta bolognese", "rundergehakt"),
    102: rec(102, "Kip uit de oven", "kipfilet"),
    103: rec(103, "Uiensoep", "uien"),
    104: rec(104, "Stamppot andijvie", "andijvie"),
}


class Ah(FakeAh):
    def __init__(self, tmp_path, **kw):
        super().__init__(tmp_path, **kw)
        self.gone: set[int] = set()

    async def get_recipe(self, id, servings):
        self._maybe_raise("get_recipe")
        if id in self.gone:
            raise AhApiError("recept niet gevonden", 404)
        r = RECIPES[id]
        # like AH: 250 g a person, so the persons show in the packs (500 g each)
        return dataclasses.replace(r, servings=servings, ingredients=[
            (f"{250 * servings} g {name}", name) for _, name in r.ingredients])


def make(tmp_path, aantal=7, away=()):
    """Seven cook evenings (no leftover nights) unless asked; W's folder is in memory."""
    ah = Ah(tmp_path)
    hass = FakeHass(tmp_path / "totaal.json", aantal=aantal)
    for dag in away:
        from app.hass import helper_persons
        hass.states[helper_persons()[dag]] = "0"
    ctx = make_ctx(tmp_path, ah=ah, hass=hass, catalog_path=tmp_path / "catalog.json")
    ctx.bonus_cache = {NEXT.start: list(BONUS)}
    return ctx, ah


def login(ah):
    ah.store.save(Tokens("secret-access-token", "secret-refresh-token", 9e9))


async def pick(client, day, rid):
    return await client.post(f"api/dish/{day.isoformat()}/pick", json={"recipe_id": rid})


# --- draft ------------------------------------------------------------------------


async def test_pick_on_an_empty_week_makes_a_draft_without_writing(aiohttp_client, tmp_path):
    ctx, ah = make(tmp_path)
    client = await aiohttp_client(make_app(ctx))
    r = await pick(client, W, 101)
    assert r.status == 200, await r.text()
    body = await r.json()
    assert body["draft"] is True and body["recipe"]["id"] == 101
    assert body["persons"] == 2 and body["date"] == W.isoformat()
    assert ah.writes == []
    log = ctx.ledger.load(W)
    assert log.plan is None and log.draft is not None
    assert [d.recipe.id for d in Plan.from_dict(log.draft).dishes] == [101]

    r = await client.get(f"api/week?week={W.isoformat()}")
    week = await r.json()
    assert week["draft"] is True
    days = {d["date"]: d for d in week["days"]}
    assert days[W.isoformat()]["recipe"]["id"] == 101
    assert days[DO.isoformat()]["kies"] is True and days[DO.isoformat()]["recipe"] is None

    # the draft dish can be opened, read only
    r = await client.get(f"api/dish/{W.isoformat()}")
    assert r.status == 200
    got = await r.json()
    assert got["draft"] is True and got["recipe"]["id"] == 101 and got["can_back"] is False


async def test_week_without_draft_lists_the_evenings_to_choose(aiohttp_client, tmp_path):
    ctx, ah = make(tmp_path, aantal=5, away=("vrijdag",))
    client = await aiohttp_client(make_app(ctx))
    week = await (await client.get(f"api/week?week={W.isoformat()}")).json()
    assert week["draft"] is True and len(week["days"]) == 7
    days = {d["date"]: d for d in week["days"]}
    assert days[VR.isoformat()]["niet_thuis"] is True and "kies" not in days[VR.isoformat()]
    assert week["niet_thuis"] == [VR.isoformat()]
    # five cooking moments over six eat days: one leftover night, never to choose
    assert sum(1 for d in week["days"] if d.get("kies")) == 5
    assert ah.writes == [] and ctx.ledger.load(W).draft is None


async def test_pick_on_an_away_evening_is_refused(aiohttp_client, tmp_path):
    ctx, ah = make(tmp_path, away=("vrijdag",))
    client = await aiohttp_client(make_app(ctx))
    r = await pick(client, VR, 101)
    assert r.status == 400
    assert (await r.json())["error"] == "Op deze avond eten jullie niet thuis"
    assert ctx.ledger.load(W).draft is None and ah.writes == []


async def test_confirm_writes_the_draft_once(aiohttp_client, tmp_path):
    ctx, ah = make(tmp_path)
    client = await aiohttp_client(make_app(ctx))
    assert (await pick(client, W, 101)).status == 200
    assert (await pick(client, DO, 102)).status == 200
    assert ah.writes == []

    url = f"api/week/confirm?week={W.isoformat()}"
    a, b = await asyncio.gather(client.post(url), client.post(url))
    assert sorted([a.status, b.status]) == [200, 409], (await a.text(), await b.text())
    ok = a if a.status == 200 else b
    week = await ok.json()
    assert week.get("draft") is not True
    assert {d["recipe"]["id"] for d in week["days"] if d.get("recipe")} == {101, 102}

    adds = [w for w in ah.writes if list(w[1].values())[0] > 0]
    assert sorted(pid for _, w in adds for pid in w) == [11, 12]  # one add per product
    assert ah.max_active_writes == 1
    log = ctx.ledger.load(W)
    assert log.plan is not None and log.draft is None
    assert log.round_done is True and log.folder_period_start == NEXT.start
    assert log.added == {11: 1, 12: 1}
    # the old round's success message
    assert any("Weekmenu klaar" in n.get("title", "") for n in ctx.hass.notifications())


async def test_confirm_without_a_draft_is_refused(aiohttp_client, tmp_path):
    ctx, ah = make(tmp_path)
    client = await aiohttp_client(make_app(ctx))
    r = await client.post(f"api/week/confirm?week={W.isoformat()}")
    assert r.status == 400
    assert (await pick(client, W, 101)).status == 200
    assert (await client.post(f"api/dish/{W.isoformat()}/remove")).status == 200
    r = await client.post(f"api/week/confirm?week={W.isoformat()}")
    assert r.status == 400  # an empty draft
    assert ah.writes == [] and ctx.ledger.load(W).plan is None


async def test_pick_after_confirm_goes_through_the_write_path(aiohttp_client, tmp_path):
    ctx, ah = make(tmp_path)
    client = await aiohttp_client(make_app(ctx))
    assert (await pick(client, W, 101)).status == 200
    r = await client.post(f"api/week/confirm?week={W.isoformat()}")
    assert r.status == 200, await r.text()
    assert ah.held() == {11: 1}

    r = await pick(client, W, 102)
    assert r.status == 200, await r.text()
    body = await r.json()
    assert body["recipe"]["id"] == 102 and "draft" not in body
    assert body["removed"] == 1 and body["target"] == "list"
    assert ah.held() == {12: 1}  # the gehakt only 101 needed went off the list
    log = ctx.ledger.load(W)
    assert log.previous[W.isoformat()] == 101 and log.swapped_out[W.isoformat()] == [101]
    assert log.added == {12: 1}

    r = await client.post(f"api/dish/{W.isoformat()}/back")
    assert r.status == 200, await r.text()
    assert (await r.json())["recipe"]["id"] == 101
    assert ah.held() == {11: 1}


async def test_remove_and_persons_on_a_draft_never_write(aiohttp_client, tmp_path):
    ctx, ah = make(tmp_path)
    client = await aiohttp_client(make_app(ctx))
    assert (await pick(client, W, 101)).status == 200
    assert (await pick(client, DO, 102)).status == 200

    r = await client.post(f"api/dish/{W.isoformat()}/persons", json={"persons": 4})
    assert r.status == 200, await r.text()
    body = await r.json()
    assert body["draft"] is True and body["persons"] == 4
    draft = Plan.from_dict(ctx.ledger.load(W).draft)
    assert {d.date: d.persons for d in draft.dishes} == {W: 4, DO: 2}

    r = await client.post(f"api/dish/{DO.isoformat()}/remove")
    assert r.status == 200, await r.text()
    assert (await r.json())["draft"] is True
    draft = Plan.from_dict(ctx.ledger.load(W).draft)
    assert [d.date for d in draft.dishes] == [W]
    assert ah.writes == [] and ctx.ledger.load(W).plan is None


async def test_persons_on_a_deleted_own_recipe_is_a_plain_409(aiohttp_client, tmp_path):
    ctx, ah = make(tmp_path)
    own = ctx.recipes.save(OwnRecipe(0, "Uiensoep van oma", 2, ["500 g uien"], ["Kook."]))
    client = await aiohttp_client(make_app(ctx))
    assert (await pick(client, W, own.id)).status == 200
    assert (await pick(client, DO, 101)).status == 200
    assert (await client.post(f"api/week/confirm?week={W.isoformat()}")).status == 200

    # an own dish keeps its copy: deleting the recipe never breaks it
    ctx.recipes.remove(own.id)
    r = await client.post(f"api/dish/{W.isoformat()}/persons", json={"persons": 4})
    assert r.status == 200, await r.text()
    assert (await r.json())["persons"] == 4

    # a recipe that is gone (404) is a plain 409, not an AH error
    ah.gone.add(101)
    r = await client.post(f"api/dish/{DO.isoformat()}/persons", json={"persons": 4})
    assert r.status == 409
    assert (await r.json())["error"].endswith("; kies een ander gerecht")

    # picking a deleted own recipe from a stale list: the same plain 409
    r = await pick(client, DO, own.id)
    assert r.status == 409
    assert (await r.json())["error"] == "Dit eigen recept bestaat niet meer; kies een ander gerecht"


# --- the choice list --------------------------------------------------------------


async def test_choose_and_search(aiohttp_client, tmp_path):
    ctx, ah = make(tmp_path)
    ctx.catalog.add(NEXT.start, [Entry(101, "Pasta bolognese", None, 30, None, None,
                                       "pasta en Italiaans", 3.0, False)])
    own = ctx.recipes.save(OwnRecipe(0, "Uiensoep van oma", 2, ["500 g uien"], ["Kook."]))
    client = await aiohttp_client(make_app(ctx))

    r = await client.get(f"api/choose?day={W.isoformat()}")
    assert r.status == 200, await r.text()
    view = await r.json()
    assert {x["id"] for x in view["items"]} == {101, own.id}
    assert view["catalog_ready"] is True and view["kinds"]

    r = await client.post("api/choose/search", json={"q": "stamppot"})
    assert r.status == 409
    assert (await r.json())["error"] == "Log eerst in bij AH om daar te zoeken"

    login(ah)
    ah.recipe_hits = {"stamppot": [104]}
    r = await client.post(f"api/choose/search?week={W.isoformat()}", json={"q": "stamppot"})
    assert r.status == 200, await r.text()
    assert [x["id"] for x in (await r.json())["items"]] == [104]
    # found once, in the list from then on
    view = await (await client.get(f"api/choose?day={W.isoformat()}&q=stamppot")).json()
    assert [x["id"] for x in view["items"]] == [104]

    ah.raise_on["search_recipes"] = AhApiError("boom", 429)  # AH refuses: blocking
    r = await client.post("api/choose/search", json={"q": "lasagne"})
    assert r.status == 502
    assert "boom" not in (await r.json())["error"]


async def test_choose_only_for_this_or_next_menu_week(aiohttp_client, tmp_path):
    ctx, ah = make(tmp_path)
    login(ah)
    client = await aiohttp_client(make_app(ctx))
    far = W + timedelta(days=14)
    assert (await client.get("api/choose?week=2026-11-18")).status == 400
    r = await client.post("api/choose/search?week=2026-11-18", json={"q": "stamppot"})
    assert r.status == 400
    assert (await pick(client, far, 101)).status == 400
    assert (await client.post("api/week/confirm?week=2026-11-18")).status == 400
    assert ah.calls == []


async def test_draft_on_a_log_from_a_newer_add_on_is_refused(aiohttp_client, tmp_path):
    ctx, ah = make(tmp_path)
    ctx.ledger.save(WeekLog(W))
    path = ctx.ledger.root / f"{W.isoformat()}.json"
    import json
    data = json.loads(path.read_text(encoding="utf-8"))
    data["schema"] = 99
    path.write_text(json.dumps(data), encoding="utf-8")
    client = await aiohttp_client(make_app(ctx))
    r = await pick(client, W, 101)
    assert r.status == 409
    assert ah.writes == []


@pytest.mark.parametrize("body", [{}, {"recipe_id": "101"}, {"recipe_id": True}])
async def test_pick_needs_a_recipe_id(aiohttp_client, tmp_path, body):
    ctx, ah = make(tmp_path)
    client = await aiohttp_client(make_app(ctx))
    r = await client.post(f"api/dish/{W.isoformat()}/pick", json=body)
    assert r.status == 400


async def test_draft_is_kept_in_the_log_round_trip(tmp_path):
    log = WeekLog(W, draft={"dishes": []})
    assert WeekLog.from_dict(log.to_dict()).draft == {"dishes": []}
    assert WeekLog.from_dict({"week": W.isoformat()}).draft is None


def test_own_recipes_from_a_newer_add_on_ask_for_an_update(tmp_path):
    from app import main
    ctx, _ = make(tmp_path)
    ctx.recipes.path.write_text('{"schema": 99, "recipes": []}', encoding="utf-8")
    main.scan_too_new(ctx)
    assert "recipes.json" in ctx.too_new and main.update_needed(ctx)


# --- fix round 1 ------------------------------------------------------------------


async def test_pick_fills_an_empty_evening_of_a_confirmed_week(aiohttp_client, tmp_path):
    ctx, ah = make(tmp_path)
    client = await aiohttp_client(make_app(ctx))
    assert (await pick(client, W, 101)).status == 200
    assert (await client.post(f"api/week/confirm?week={W.isoformat()}")).status == 200
    assert ah.held() == {11: 1}
    week = await (await client.get(f"api/week?week={W.isoformat()}")).json()
    days = {d["date"]: d for d in week["days"]}
    assert days[DO.isoformat()].get("kies") is True and "kies" not in days[W.isoformat()]

    r = await pick(client, DO, 102)
    assert r.status == 200, await r.text()
    assert (await r.json())["recipe"]["id"] == 102
    assert ah.held() == {11: 1, 12: 1}  # the first evening's gehakt is not lowered
    assert not [w for w in ah.writes if list(w[1].values())[0] < 0]
    log = ctx.ledger.load(W)
    assert DO.isoformat() not in log.previous and DO.isoformat() not in log.previous_dish
    assert log.added == {11: 1, 12: 1}


async def test_remove_after_confirm_then_pick_the_same_evening(aiohttp_client, tmp_path):
    ctx, ah = make(tmp_path)
    client = await aiohttp_client(make_app(ctx))
    assert (await pick(client, W, 101)).status == 200
    assert (await pick(client, DO, 102)).status == 200
    assert (await client.post(f"api/week/confirm?week={W.isoformat()}")).status == 200
    r = await client.post(f"api/dish/{DO.isoformat()}/remove")
    assert r.status == 200, await r.text()
    assert ah.held() == {11: 1}
    week = await (await client.get(f"api/week?week={W.isoformat()}")).json()
    assert {d["date"]: d for d in week["days"]}[DO.isoformat()].get("kies") is True
    r = await pick(client, DO, 103)
    assert r.status == 200, await r.text()
    assert ah.held() == {11: 1, 13: 1}
    plan = Plan.from_dict(ctx.ledger.load(W).plan)
    assert DO not in plan.removed_days


async def test_pick_on_a_confirmed_week_checks_the_evening(aiohttp_client, tmp_path):
    ctx, ah = make(tmp_path, aantal=6, away=("vrijdag",))  # six cook evenings
    client = await aiohttp_client(make_app(ctx))
    assert (await pick(client, W + timedelta(days=3), 101)).status == 200
    assert (await client.post(f"api/week/confirm?week={W.isoformat()}")).status == 200
    r = await pick(client, VR, 102)
    assert r.status == 400 and (await r.json())["error"] == "Op deze avond eten jullie niet thuis"


async def test_pick_on_a_leftover_night_says_restjes(aiohttp_client, tmp_path):
    ctx, ah = make(tmp_path, aantal=5, away=("vrijdag",))  # W cooks, DO is its leftover
    client = await aiohttp_client(make_app(ctx))
    r = await pick(client, DO, 101)
    assert r.status == 400 and (await r.json())["error"] == "Op deze avond eten jullie restjes"
    assert (await pick(client, W, 101)).status == 200
    assert (await client.post(f"api/week/confirm?week={W.isoformat()}")).status == 200
    r = await pick(client, DO, 102)  # the leftover night of W's dish
    assert r.status == 400 and (await r.json())["error"] == "Op deze avond eten jullie restjes"


async def test_pick_when_home_assistant_cannot_be_read_is_a_503(aiohttp_client, tmp_path):
    ctx, ah = make(tmp_path)
    client = await aiohttp_client(make_app(ctx))
    assert (await pick(client, W, 101)).status == 200
    ctx.hass.fail_reads = True
    r = await pick(client, DO, 102)
    assert r.status == 503
    assert (await r.json())["error"] == "Home Assistant is nu niet te lezen; probeer het zo opnieuw"
    ctx.hass.fail_reads = False
    assert (await client.post(f"api/week/confirm?week={W.isoformat()}")).status == 200
    ctx.hass.fail_reads = True
    r = await pick(client, DO, 102)  # an empty evening of a confirmed week
    assert r.status == 503
    assert ah.held() == {11: 1}


async def test_confirm_drops_dishes_on_evenings_now_away(aiohttp_client, tmp_path):
    from app.hass import helper_persons
    ctx, ah = make(tmp_path)
    client = await aiohttp_client(make_app(ctx))
    assert (await pick(client, W, 101)).status == 200
    assert (await pick(client, VR, 102)).status == 200
    ctx.hass.states[helper_persons()["vrijdag"]] = "0"  # not at home on Friday after all
    r = await client.post(f"api/week/confirm?week={W.isoformat()}")
    assert r.status == 200, await r.text()
    body = await r.json()
    assert body["dropped"] == [VR.isoformat()]
    assert ah.held() == {11: 1}
    plan = Plan.from_dict(ctx.ledger.load(W).plan)
    assert [d.date for d in plan.dishes] == [W] and VR not in plan.removed_days
    assert VR.isoformat() in body["niet_thuis"]


async def test_confirm_with_only_away_dishes_is_an_empty_draft(aiohttp_client, tmp_path):
    from app.hass import helper_persons
    ctx, ah = make(tmp_path)
    client = await aiohttp_client(make_app(ctx))
    assert (await pick(client, VR, 102)).status == 200
    ctx.hass.states[helper_persons()["vrijdag"]] = "0"
    r = await client.post(f"api/week/confirm?week={W.isoformat()}")
    assert r.status == 400
    assert ah.writes == [] and ctx.ledger.load(W).plan is None


# --- fix round 2 ------------------------------------------------------------------


async def test_a_leftover_night_with_its_own_dish_is_never_claimed(aiohttp_client, tmp_path):
    """The helpers changed after DO got a dish: W now has DO as its leftover night.
    Filling W cooks for W alone and leaves DO's dish alone (confirmed and draft)."""
    ctx, ah = make(tmp_path)
    client = await aiohttp_client(make_app(ctx))
    assert (await pick(client, DO, 102)).status == 200
    assert (await client.post(f"api/week/confirm?week={W.isoformat()}")).status == 200
    ctx.hass.states["input_number.weekmenu_avonden"] = "6"  # (W, DO) is a pair now
    r = await pick(client, W, 101)
    assert r.status == 200, await r.text()
    body = await r.json()
    assert body["persons"] == 2 and body["restjes"] is None
    assert ah.held() == {12: 1, 11: 1}  # one pack: W's own two persons, not four
    plan = Plan.from_dict(ctx.ledger.load(W).plan)
    assert {d.date: d.leftover_date for d in plan.dishes} == {W: None, DO: None}

    # the same in a draft of the next week
    nxt = W + timedelta(days=7)
    ctx.clock = lambda: SUNDAY + timedelta(days=7)
    ctx.hass.states["input_number.weekmenu_avonden"] = "7"
    assert (await pick(client, nxt + timedelta(days=1), 102)).status == 200
    ctx.hass.states["input_number.weekmenu_avonden"] = "6"
    r = await pick(client, nxt, 101)
    assert r.status == 200, await r.text()
    body = await r.json()
    assert body["persons"] == 2 and body["restjes"] is None and body["draft"] is True


async def test_pick_on_an_evening_without_a_cooking_moment(aiohttp_client, tmp_path):
    ctx, ah = make(tmp_path, aantal=2)  # (W, DO), (VR, ZA); ZO, MA and DI get nothing
    client = await aiohttp_client(make_app(ctx))
    r = await pick(client, W + timedelta(days=4), 101)
    assert r.status == 400
    assert (await r.json())["error"] == "Op deze avond staat geen kookmoment"
    r = await pick(client, DO, 101)
    assert (await r.json())["error"] == "Op deze avond eten jullie restjes"


async def test_draft_says_where_confirming_would_write(aiohttp_client, tmp_path):
    """Fix round 1: a draft's "target" is where confirming would write now (an active
    order: "order", else "list"), asked of AH at most every 5 minutes, and null without
    a login or when AH does not answer (the app then shows a neutral button)."""
    from app.ahclient import ActiveOrder
    from app.web import DRAFT_TARGET_TTL

    ctx, ah = make(tmp_path)
    client = await aiohttp_client(make_app(ctx))

    async def target():
        week = await (await client.get(f"api/week?week={W.isoformat()}")).json()
        assert week["draft"] is True
        return week["target"]

    assert await target() is None  # not logged in: never asked
    login(ah)
    assert await target() == "list"
    ah.order = ActiveOrder(7, {}, W + timedelta(days=1))
    assert await target() == "list"  # kept for a while: no AH read on every look
    stamp, kept = ctx.draft_targets[W]
    ctx.draft_targets[W] = (stamp - DRAFT_TARGET_TTL - 1, kept)
    assert await target() == "order"
    ctx.draft_targets.clear()
    ah.raise_on["active_order"] = AhApiError("boom", 500)
    assert await target() is None
    assert W not in ctx.draft_targets  # an error is not kept: the next look asks again
    assert ah.writes == []


# --- final review -----------------------------------------------------------------

OLD_GEHAKT = Product(21, "AH Rundergehakt", "500 g", 7.0, 7.0, True, "25% korting", True,
                     "Vlees")


def only_current(ctx, ah):
    """Only the running folder is out: the menu week falls back on it."""
    from tests.fakes import CURRENT
    ah.periods = [CURRENT]
    ctx.bonus_cache = {CURRENT.start: [OLD_GEHAKT, KIP, UI], NEXT.start: list(BONUS)}


async def test_draft_picked_on_the_old_folder_is_matched_again_on_confirm(aiohttp_client,
                                                                         tmp_path):
    from tests.fakes import CURRENT
    ctx, ah = make(tmp_path)
    only_current(ctx, ah)
    client = await aiohttp_client(make_app(ctx))
    assert (await pick(client, W, 101)).status == 200
    assert ctx.ledger.load(W).draft_period_start == CURRENT.start
    draft = Plan.from_dict(ctx.ledger.load(W).draft)
    assert [ln.product.id for ln in draft.dishes[0].lines] == [21]

    ah.periods = [CURRENT, NEXT]  # the week's own folder came out before confirming
    r = await client.post(f"api/week/confirm?week={W.isoformat()}")
    assert r.status == 200, await r.text()
    assert ah.held() == {11: 1}  # NEXT's gehakt, not the old folder's
    log = ctx.ledger.load(W)
    assert log.folder_period_start == NEXT.start and log.period_warning is None
    assert log.draft_period_start is None
    plan = Plan.from_dict(log.plan)
    assert [ln.product.id for ln in plan.dishes[0].lines] == [11]


async def test_a_later_pick_on_a_newer_folder_keeps_the_oldest_period(aiohttp_client,
                                                                     tmp_path):
    from tests.fakes import CURRENT
    ctx, ah = make(tmp_path)
    only_current(ctx, ah)
    client = await aiohttp_client(make_app(ctx))
    assert (await pick(client, W, 101)).status == 200
    ah.periods = [CURRENT, NEXT]
    assert (await pick(client, DO, 102)).status == 200
    assert ctx.ledger.load(W).draft_period_start == CURRENT.start  # W still on the old one
    assert (await client.post(f"api/dish/{W.isoformat()}/remove")).status == 200
    assert (await client.post(f"api/dish/{DO.isoformat()}/remove")).status == 200
    assert (await pick(client, DO, 102)).status == 200  # an empty draft starts again
    assert ctx.ledger.load(W).draft_period_start == NEXT.start


async def test_confirmed_on_the_old_folder_offers_menu_bijwerken_later(aiohttp_client,
                                                                      tmp_path):
    from app import main
    from app.main import PERIOD_WARNING
    from tests.fakes import CURRENT
    ctx, ah = make(tmp_path)
    only_current(ctx, ah)
    client = await aiohttp_client(make_app(ctx))
    assert (await pick(client, W, 101)).status == 200
    r = await client.post(f"api/week/confirm?week={W.isoformat()}")
    assert r.status == 200, await r.text()
    assert ah.held() == {21: 1}
    log = ctx.ledger.load(W)
    assert log.folder_period_start == CURRENT.start and log.period_warning == PERIOD_WARNING

    ah.periods = [CURRENT, NEXT]  # the week's own folder comes out
    await main.check_new_folder(ctx, W)
    log = ctx.ledger.load(W)
    from app.ledger import new_folder_available
    assert new_folder_available(log) is True
