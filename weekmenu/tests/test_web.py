import asyncio
import logging
from datetime import date, datetime

import pytest

from app import main
from app.ahclient import AhApiError
from app.hass import HELPER_AVONDEN, helper_persons
from app.ledger import WeekLog
from app import web
from app.web import make_app
from tests.fakes import SUNDAY, W, FakeAh, dish, make_ctx, make_plan

WO, DO, VR = W, date(2026, 11, 5), date(2026, 11, 6)
CODE = "Ab12-cD34-eF56"


def week_plan():
    return make_plan([dish(WO, 101, [1, 2], 2.0), dish(DO, 102, [2, 3], 3.0)])


@pytest.fixture
def ctx(tmp_path):
    c = make_ctx(tmp_path)
    log = WeekLog(W)
    c.ledger.save(log)
    return c


async def planned_ctx(ctx):
    """A week whose menu is already on the AH list (like after the Sunday round)."""
    from app.writer import apply
    await apply(ctx.client, ctx.ledger, ctx.ledger.load(W), week_plan())
    return ctx


@pytest.fixture
def swaps(monkeypatch):
    """"Ander gerecht" (a pick from the choice list) and restore_dish without
    Allerhande: fixed replacement dishes, 301 on Wednesday and 302 on Thursday."""
    entered = []

    async def fake_pick(client, plan, day, recipe_id, bonus, persons, leftover_date,
                        overrides=None):
        entered.append(day)
        await asyncio.sleep(0.02)
        new = {WO: dish(WO, 301, [1, 9], 1.5), DO: dish(DO, 302, [2, 9], 1.5)}[day]
        assert new.recipe.id == recipe_id
        return make_plan([d for d in plan.dishes if d.date != day] + [new])

    async def fake_restore(client, plan, day, recipe_id, bonus, overrides=None):
        old = {101: dish(WO, 101, [1, 2], 2.0), 102: dish(DO, 102, [2, 3], 3.0)}[recipe_id]
        return make_plan([d for d in plan.dishes if d.date != day] + [old])

    monkeypatch.setattr(web, "pick_dish", fake_pick)
    monkeypatch.setattr(main, "restore_dish", fake_restore)
    return entered


def swap(client, day):
    """"Ander gerecht" on `day`: the replacement dish of the swaps fixture is picked."""
    return client.post(f"api/dish/{day.isoformat()}/pick",
                       json={"recipe_id": {WO: 301, DO: 302}[day]})


def with_new_folder(ctx):
    """The week's menu was confirmed with the current folder; its own folder is out."""
    ctx.ledger.save(WeekLog(W, plan=week_plan().to_dict(), period_warning=main.PERIOD_WARNING,
                            folder_period_start=date(2026, 10, 26),
                            new_folder=date(2026, 11, 2)))


# --- the brief's tests -------------------------------------------------------------


async def test_web_post_code_extracts_code_from_full_line(aiohttp_client, ctx):
    client = await aiohttp_client(make_app(ctx))
    r = await client.get("api/status")
    assert (await r.json())["logged_in"] is False
    line = f"Failed to launch 'appie://login-exit?code={CODE}' because the scheme does not"
    r = await client.post("api/login", json={"code": line})
    assert r.status == 200
    assert ctx.client.codes == [CODE]
    r = await client.get("api/status")
    assert (await r.json())["logged_in"] is True

    r = await client.post("api/login", json={"code": "   "})
    assert r.status == 400


async def test_web_never_echoes_code_or_token(aiohttp_client, ctx, caplog):
    caplog.set_level(logging.DEBUG)
    client = await aiohttp_client(make_app(ctx))
    texts = []
    r = await client.post("api/login", json={"code": f"appie://login-exit?code={CODE}"})
    texts.append(await r.text())
    for path in ("api/status", "api/week", "api/savings", "api/settings", ""):
        texts.append(await (await client.get(path)).text())
    # a failing exchange does not echo the code either
    ctx.client.raise_on["exchange_code"] = AhApiError("token request failed: HTTP 400", 400)
    r = await client.post("api/login", json={"code": CODE})
    assert r.status == 400
    texts.append(await r.text())
    blob = "\n".join(texts) + caplog.text
    for secret in (CODE, "secret-access-token", "secret-refresh-token"):
        assert secret not in blob


async def test_api_swap_then_back_cleans_up_own_products(aiohttp_client, ctx, swaps):
    await planned_ctx(ctx)
    ah = ctx.client
    ah.listed[4] = 1  # the user's own
    assert ah.held() == {1: 1, 2: 2, 3: 1, 4: 1}
    client = await aiohttp_client(make_app(ctx))

    r = await swap(client, WO)
    assert r.status == 200, await r.text()
    body = await r.json()
    assert body["recipe"]["id"] == 301
    # 101 used 1 and 2; 301 uses 1 and 9: the 2 only 101 needed goes off the list
    assert ah.held() == {1: 1, 2: 1, 3: 1, 4: 1, 9: 1}
    assert body["removed"] == 1 and body["target"] == "list" and body["leftovers"] == []
    log = ctx.ledger.load(W)
    assert log.swapped_out[WO.isoformat()] == [101] and log.previous[WO.isoformat()] == 101

    r = await client.post(f"api/dish/{WO.isoformat()}/back")
    assert r.status == 200, await r.text()
    body = await r.json()
    assert body["recipe"]["id"] == 101 and body["removed"] == 1
    assert ah.held() == {1: 1, 2: 2, 3: 1, 4: 1}  # 9 went off again, 4 never touched
    assert ctx.ledger.load(W).added == {1: 1, 2: 2, 3: 1}
    log = ctx.ledger.load(W)
    assert WO.isoformat() not in log.previous
    assert 301 in log.swapped_out[WO.isoformat()]

    # no second "back"
    r = await client.post(f"api/dish/{WO.isoformat()}/back")
    assert r.status == 409


async def test_api_writes_are_serialized(aiohttp_client, ctx, swaps):
    entered = swaps
    await planned_ctx(ctx)
    ah = ctx.client
    ah.max_active_writes = 0
    client = await aiohttp_client(make_app(ctx))
    r1, r2 = await asyncio.gather(
        swap(client, WO),
        swap(client, DO),
    )
    assert r1.status == 200 and r2.status == 200
    assert ah.max_active_writes == 1
    assert sorted(entered) == [WO, DO]
    # the second swap saw the first one's menu and log: both new dishes stay, and
    # product 9 (one pack per dish) is bought exactly twice, once per swap
    log = ctx.ledger.load(W)
    assert sorted(d["recipe"]["id"] for d in log.plan["dishes"]) == [301, 302]
    assert ah.held()[9] == 2 and log.added[9] == 2
    assert [d for k, d in ah.writes if 9 in d] == [{9: 1}, {9: 1}]
    assert set(log.previous) == {WO.isoformat(), DO.isoformat()}


async def test_api_settings_rejects_unknown_entity(aiohttp_client, ctx):
    client = await aiohttp_client(make_app(ctx))
    r = await client.post("api/settings", json={"entity_id": "light.keuken", "value": 1})
    assert r.status == 400
    assert ctx.hass.posts == []
    r = await client.post("api/settings", json={"entity_id": HELPER_AVONDEN, "value": 4})
    assert r.status == 200
    assert ctx.hass.posts == [
        ("services/input_number/set_value", {"entity_id": HELPER_AVONDEN, "value": 4})
    ]


# --- extra coverage ----------------------------------------------------------------


async def test_api_week_and_dish_shape(aiohttp_client, ctx):
    await planned_ctx(ctx)
    ctx.hass.states["input_number.weekmenu_personen_vrijdag"] = "0"
    client = await aiohttp_client(make_app(ctx))
    body = await (await client.get("api/week")).json()
    assert body["week"] == W.isoformat() and body["target"] == "list"
    assert body["savings"] == 5.0 and body["products"] == 3 and body["bonus_products"] == 3
    days = body["days"]
    # every day of the week; eat days without a dish are "geen kookmoment"
    assert [d["date"] for d in days] == [f"2026-11-{n:02d}" for n in range(4, 11)]
    assert all(d.get("geen_kookmoment") for d in days[3:])
    assert days[0]["dag"] == "woensdag" and days[0]["recipe"]["title"] == "Recept 101"
    assert set(days[0]["recipe"]) == {"id", "title", "image", "cook", "oven", "wait",
                                      "cuisine", "url"}
    assert days[2]["niet_thuis"] is True and days[2]["recipe"] is None
    assert "ouders" not in body and all("ouders" not in d for d in days)

    d = await (await client.get(f"api/dish/{WO.isoformat()}")).json()
    assert d["steps"] == ["Snijd de ui."]
    assert {"product": "Product 1", "qty": 1, "mechanism": "2e halve prijs",
            "pantry": False} .items() <= d["lines"][0].items()
    assert d["lines"][-1]["pantry"] is True
    assert (await client.get("api/dish/2026-11-09")).status == 404
    assert (await client.get("api/dish/gisteren")).status == 400


async def test_api_week_shows_running_week_if_planned(aiohttp_client, tmp_path):
    ctx = make_ctx(tmp_path, now=datetime(2026, 11, 8, 12, 0))  # Sunday in the week of W
    client = await aiohttp_client(make_app(ctx))
    body = await (await client.get("api/week")).json()
    # nothing yet: next week, its evenings to choose for (v1.1)
    assert body["week"] == "2026-11-11" and body["draft"] is True
    assert not any(d["recipe"] for d in body["days"])
    await planned_ctx(ctx)
    body = await (await client.get("api/week")).json()
    assert body["week"] == W.isoformat()


async def test_api_persons_rescales_only_that_dish(aiohttp_client, ctx, monkeypatch):
    await planned_ctx(ctx)
    seen = []

    async def fake_rescale(client, plan, day, persons, overrides=None):
        seen.append((day, persons))
        return make_plan([dish(WO, 101, [1, 2, 7], 2.0, persons=persons)]
                         + [d for d in plan.dishes if d.date != WO])

    monkeypatch.setattr(main, "rescale_dish", fake_rescale)
    client = await aiohttp_client(make_app(ctx))
    r = await client.post(f"api/dish/{WO.isoformat()}/persons", json={"persons": 4})
    assert r.status == 200 and seen == [(WO, 4)]
    assert ctx.client.held()[7] == 1
    assert ctx.hass.posts == [] or all("set_value" not in p for p, _ in ctx.hass.posts)
    assert (await client.post(f"api/dish/{WO.isoformat()}/persons",
                              json={"persons": 0})).status == 400


async def test_api_back_refused_once_week_started(aiohttp_client, tmp_path, swaps):
    ctx = make_ctx(tmp_path)
    await planned_ctx(ctx)
    client = await aiohttp_client(make_app(ctx))
    assert (await swap(client, WO)).status == 200
    ctx.clock = lambda: datetime(2026, 11, 4, 18, 0)
    r = await client.post(f"api/dish/{WO.isoformat()}/back")
    assert r.status == 409


async def test_api_run_starts_round_once(aiohttp_client, ctx, monkeypatch):
    started = asyncio.Event()
    release = asyncio.Event()

    async def fake_round(c, now, mode):
        started.set()
        await release.wait()

    monkeypatch.setattr(main, "run_round", fake_round)
    with_new_folder(ctx)
    client = await aiohttp_client(make_app(ctx))
    for mode in ("fout", "nu_maken", "nieuw_menu"):  # v1.1: the menu is chosen in the app
        r = await client.post("api/run", json={"mode": mode})
        assert r.status == 400 and "bijwerken" in (await r.json())["error"]
    r = await client.post("api/run", json={"mode": "bijwerken"})
    assert r.status == 202
    r = await client.post("api/run", json={"mode": "bijwerken"})
    assert r.status == 409
    await started.wait()
    release.set()
    await asyncio.sleep(0.01)
    assert (await (await client.get("api/status")).json())["running"] is False


async def test_api_savings(aiohttp_client, ctx):
    (ctx.total_path).write_text('{"total": 12.5}', encoding="utf-8")
    ctx.ledger.save(WeekLog(date(2026, 10, 28), plan=week_plan().to_dict(), savings=4.0,
                            counted=True))
    ctx.ledger.save(WeekLog(W, plan=week_plan().to_dict(), savings=3.0, counted=True))
    ctx.clock = lambda: datetime(2026, 11, 8, 12, 0)
    client = await aiohttp_client(make_app(ctx))
    body = await (await client.get("api/savings")).json()
    assert body["total"] == 12.5 and body["month"] == 3.0  # only the November week
    assert {"week": "2026-10-28", "savings": 4.0, "counted": True,
            "reconciled": None} in body["weeks"]


async def test_api_login_expired_is_reported(aiohttp_client, ctx, swaps):
    from app.ahclient import AhLoginExpired
    await planned_ctx(ctx)
    ctx.client.raise_on["bonus_periods"] = AhLoginExpired("login expired", 401)
    client = await aiohttp_client(make_app(ctx))
    r = await swap(client, WO)
    assert r.status == 401
    assert (await (await client.get("api/status")).json())["logged_in"] is False


async def test_only_ingress_proxy_may_connect(aiohttp_client, ctx):
    client = await aiohttp_client(make_app(ctx, allowed_remotes={"172.30.32.2"}))
    assert (await client.get("api/status")).status == 403


async def test_index_without_static_is_a_login_page(aiohttp_client, ctx, tmp_path):
    client = await aiohttp_client(make_app(ctx, static_dir=tmp_path / "geen"))
    r = await client.get("")
    assert r.status == 200 and "appie://login-exit" in await r.text()
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("<p>app</p>", encoding="utf-8")
    (static / "app.css").write_text("body{}", encoding="utf-8")
    client = await aiohttp_client(make_app(ctx, static_dir=static))
    assert await (await client.get("")).text() == "<p>app</p>"
    assert (await client.get("app.css")).status == 200
    assert (await client.get("static/app.css")).status == 200
    assert (await client.get("../tokens.json")).status == 404


# --- fix round 1 (Ruling 22) -------------------------------------------------------


async def test_api_back_restores_exact_dish_without_ah_reads(aiohttp_client, ctx, swaps,
                                                             monkeypatch):
    import dataclasses
    from app.writer import apply

    async def no_restore(*a, **k):
        raise AssertionError("must not look the recipe up again")

    monkeypatch.setattr(main, "restore_dish", no_restore)
    original = dataclasses.replace(dish(WO, 101, [1, 2], 2.0), oven=20, wait=60)
    from app.planner import put_dish  # real plans come out of _finish: do the same here
    start = put_dish(make_plan([dish(DO, 102, [2, 3], 3.0)]), original)
    await apply(ctx.client, ctx.ledger, ctx.ledger.load(W), start)
    client = await aiohttp_client(make_app(ctx))
    before = (await (await client.get(f"api/dish/{WO.isoformat()}")).json())
    assert (await swap(client, WO)).status == 200
    ah = ctx.client
    reads, writes = list(ah.calls), list(ah.writes)

    r = await client.post(f"api/dish/{WO.isoformat()}/back")
    assert r.status == 200, await r.text()
    after = await r.json()
    for key in ("recipe", "lines", "persons", "steps", "not_found", "savings"):
        assert after[key] == before[key], key
    assert after["recipe"]["oven"] == 20 and after["recipe"]["wait"] == 60
    assert ah.calls == reads  # no bonus, recipe or product reads
    # the swap took 2 off and put 9 on; going back undoes exactly that, nothing else
    assert ah.writes == writes + [("list", {2: 1}), ("list", {9: -1})]
    assert ctx.ledger.load(W).previous_dish == {}


async def test_api_back_falls_back_for_logs_without_saved_dish(aiohttp_client, ctx, swaps):
    await planned_ctx(ctx)
    client = await aiohttp_client(make_app(ctx))
    assert (await swap(client, WO)).status == 200
    log = ctx.ledger.load(W)
    log.previous_dish = {}  # a log written before previous_dish existed
    ctx.ledger.save(log)
    r = await client.post(f"api/dish/{WO.isoformat()}/back")
    assert r.status == 200 and (await r.json())["recipe"]["id"] == 101


async def test_api_week_reports_budget_exhausted(aiohttp_client, ctx):
    plan = week_plan()
    plan.budget_exhausted = True
    log = ctx.ledger.load(W)
    log.plan = plan.to_dict()
    ctx.ledger.save(log)
    client = await aiohttp_client(make_app(ctx))
    body = await (await client.get("api/week")).json()
    assert body["budget_exhausted"] is True


async def test_api_week_reports_period_warning(aiohttp_client, ctx):
    log = ctx.ledger.load(W)
    log.plan = week_plan().to_dict()
    log.period_warning = main.PERIOD_WARNING
    ctx.ledger.save(log)
    client = await aiohttp_client(make_app(ctx))
    body = await (await client.get("api/week")).json()
    assert body["period_warning"] == main.PERIOD_WARNING
    log.period_warning = None
    ctx.ledger.save(log)
    body = await (await client.get("api/week")).json()
    assert body["period_warning"] is None


# --- schedule change (2 November 2026) ------------------------------------------------


async def test_api_week_and_run_bijwerken_with_the_new_folder(aiohttp_client, tmp_path,
                                                              monkeypatch):
    from app.ahclient import BonusPeriod
    rebuilt = []

    async def fake_rebuild(client, plan, bonus, overrides=None):
        rebuilt.append([d.recipe.id for d in plan.dishes])
        return make_plan([dish(WO, 101, [1, 4], 1.0), dish(DO, 102, [5], 2.0)])

    monkeypatch.setattr(main, "rebuild_plan", fake_rebuild)
    cur = BonusPeriod(date(2026, 10, 26), date(2026, 11, 1), date(2026, 10, 30), ["Vlees"])
    nxt = BonusPeriod(date(2026, 11, 2), date(2026, 11, 8), date(2026, 11, 6), ["Vlees"])
    ah = FakeAh(tmp_path, periods=[cur])
    ctx = make_ctx(tmp_path, ah=ah, now=datetime(2026, 10, 29, 10, 0))
    # confirmed on Thursday, with the current folder
    ah.listed = dict(week_plan().basket)
    ctx.ledger.save(WeekLog(W, plan=week_plan().to_dict(), added=dict(week_plan().basket),
                            target="list", added_to="list", round_done=True, notified=True,
                            period_warning=main.PERIOD_WARNING, folder_period_start=cur.start))
    client = await aiohttp_client(make_app(ctx))
    # no new folder yet: nothing to update
    r = await client.post("api/run", json={"mode": "bijwerken"})
    assert r.status == 409 and "nieuwe folder" in (await r.json())["error"]
    assert (await (await client.get("api/week")).json())["new_folder_available"] is False
    ah.periods = [cur, nxt]
    ctx.clock = lambda: datetime(2026, 10, 30, 9, 0)
    await main.tick(ctx, ctx.clock())
    body = await (await client.get("api/week")).json()
    assert body["new_folder_available"] is True and body["new_folder_week"] == "2026-11-04"
    assert (await client.post("api/run", json={"mode": "bijwerken"})).status == 202
    await asyncio.gather(*ctx.tasks)
    body = await (await client.get("api/week")).json()
    assert body["new_folder_available"] is False and body["period_warning"] is None
    # the same recipes, matched to the new folder
    assert rebuilt == [[101, 102]]
    assert [d["recipe"]["id"] for d in body["days"] if d.get("recipe")] == [101, 102]
    assert ah.held() == {1: 1, 4: 1, 5: 1}  # what only the old matching needed went off
    assert ah.offers_for == [nxt]


async def test_api_week_flags_the_new_folder_while_the_running_week_shows(aiohttp_client,
                                                                          tmp_path):
    """Thursday 5 November: the app shows the running week; the flag is about the next."""
    ctx = make_ctx(tmp_path, now=datetime(2026, 11, 5, 10, 0))
    ctx.ledger.save(WeekLog(W, plan=week_plan().to_dict()))
    nov11 = date(2026, 11, 11)
    ctx.ledger.save(WeekLog(nov11, plan=week_plan().to_dict(),
                            period_warning=main.PERIOD_WARNING,
                            folder_period_start=date(2026, 11, 2),
                            new_folder=date(2026, 11, 9)))
    client = await aiohttp_client(make_app(ctx))
    body = await (await client.get("api/week")).json()
    assert body["week"] == "2026-11-04"
    assert body["new_folder_available"] is True and body["new_folder_week"] == "2026-11-11"


async def test_api_week_names_the_order_and_no_waiting_once_planned(aiohttp_client, ctx):
    await planned_ctx(ctx)
    ctx.waiting = main.Waiting(W, date(2026, 10, 30))  # e.g. from before a restart
    client = await aiohttp_client(make_app(ctx))
    body = await (await client.get("api/week")).json()
    assert body["bestelling"] == "Voor de bestelling van woensdag 4 november"
    assert body["waiting_for_folder"] is None
    assert (await (await client.get("api/status")).json())["waiting_for_folder"] is None


async def test_api_week_says_whether_the_weekmenu_is_on(aiohttp_client, ctx):
    client = await aiohttp_client(make_app(ctx))
    assert (await (await client.get("api/week")).json())["enabled"] is True
    ctx.hass.states["input_boolean.weekmenu_aan"] = "off"
    assert (await (await client.get("api/week")).json())["enabled"] is False
    ctx.hass.fail_reads = True
    assert (await (await client.get("api/week")).json())["enabled"] is None


def test_no_em_dash_in_user_texts():
    from pathlib import Path
    root = Path(__file__).parent.parent
    files = [root / "app" / "static" / n for n in ("app.js", "index.html", "app.css")]
    files += [root / "app" / "main.py", root / "app" / "hass.py", root / "app" / "web.py",
              root.parent / "README.md"]
    for f in files:
        text = f.read_text(encoding="utf-8")
        assert "\u2014" not in text and "\\u2014" not in text, f.name


async def test_api_week_marks_the_evenings_not_at_home(aiohttp_client, ctx):
    await planned_ctx(ctx)
    ctx.hass.states["input_number.weekmenu_personen_zaterdag"] = "0"
    ctx.hass.states["input_number.weekmenu_personen_maandag"] = "0"
    client = await aiohttp_client(make_app(ctx))
    body = await (await client.get("api/week")).json()
    assert body["niet_thuis"] == ["2026-11-07", "2026-11-09"]
    assert body["geen_avonden"] is False
    away = [d for d in body["days"] if d["niet_thuis"]]
    assert [d["date"] for d in away] == ["2026-11-07", "2026-11-09"]
    assert all(d["recipe"] is None and d["persons"] == 0 for d in away)
    assert all(d["niet_thuis"] is False for d in body["days"] if d["recipe"])


async def test_api_week_without_a_menu_says_when_nobody_eats_at_home(aiohttp_client, ctx):
    for eid in helper_persons().values():
        ctx.hass.states[eid] = "0"
    client = await aiohttp_client(make_app(ctx))
    body = await (await client.get("api/week")).json()
    assert body["geen_avonden"] is True
    # v1.1: the seven evenings, all away, none to choose for
    assert all(d["niet_thuis"] and not d.get("kies") for d in body["days"])
    assert len(body["niet_thuis"]) == 7


async def test_api_settings_accepts_zero_persons(aiohttp_client, ctx):
    client = await aiohttp_client(make_app(ctx))
    eid = helper_persons()["vrijdag"]
    r = await client.post("api/settings", json={"entity_id": eid, "value": 0})
    assert r.status == 200
    assert ctx.hass.posts[-1] == ("services/input_number/set_value", {"entity_id": eid, "value": 0})


async def test_api_week_niet_thuis_skips_days_with_a_dish(aiohttp_client, ctx):
    await planned_ctx(ctx)  # dishes on wo, do (and one more)
    ctx.hass.states["input_number.weekmenu_personen_vrijdag"] = "0"
    ctx.hass.states["input_number.weekmenu_personen_donderdag"] = "0"  # still has a dish
    ctx.hass.states["input_number.weekmenu_personen_zondag"] = "0"
    client = await aiohttp_client(make_app(ctx))
    body = await (await client.get("api/week")).json()
    dish_days = {d["date"] for d in body["days"] if d["recipe"]}
    assert "2026-11-05" in dish_days
    assert body["niet_thuis"] == ["2026-11-06", "2026-11-08"]


# --- Niet thuis eten ---------------------------------------------------------------------


async def test_api_settings_away_taps_remember_and_restore(aiohttp_client, ctx):
    client = await aiohttp_client(make_app(ctx))
    eid = helper_persons()["vrijdag"]
    ctx.hass.states[eid] = "4"
    r = await client.post("api/settings", json={"entity_id": eid, "away": True})
    assert r.status == 200 and (await r.json())["value"] == 0
    assert ctx.hass.posts[-1] == ("services/input_number/set_value", {"entity_id": eid, "value": 0})
    ctx.hass.states[eid] = "0"
    settings = await (await client.get("api/settings")).json()
    assert settings["away"] == ["vrijdag"] and "ouders" not in settings
    assert "ouders_options" not in settings and "ouders" not in settings["entities"]
    r = await client.post("api/settings", json={"entity_id": eid, "away": False})
    assert (await r.json())["value"] == 4
    assert ctx.hass.posts[-1] == ("services/input_number/set_value", {"entity_id": eid, "value": 4})


async def test_api_settings_helper_set_to_zero_outside_shows_as_away(aiohttp_client, ctx):
    ctx.hass.states[helper_persons()["dinsdag"]] = "0"
    ctx.hass.states[helper_persons()["woensdag"]] = "0"
    client = await aiohttp_client(make_app(ctx))
    settings = await (await client.get("api/settings")).json()
    assert settings["away"] == ["woensdag", "dinsdag"]  # in app order, wo first


async def test_api_settings_persons_outside_1_to_8_are_refused(aiohttp_client, ctx):
    client = await aiohttp_client(make_app(ctx))
    eid = helper_persons()["vrijdag"]
    for bad in (9, -1, 2.5, "3", True):
        r = await client.post("api/settings", json={"entity_id": eid, "value": bad})
        assert r.status == 400, bad
    assert ctx.hass.posts == []
    r = await client.post("api/settings", json={"entity_id": eid, "value": 8})
    assert r.status == 200


async def test_api_settings_away_flag_is_only_for_person_helpers(aiohttp_client, ctx):
    client = await aiohttp_client(make_app(ctx))
    r = await client.post("api/settings", json={"entity_id": HELPER_AVONDEN, "away": True})
    assert r.status == 400 and ctx.hass.posts == []


# --- opschonen and "Gerecht weghalen" ---------------------------------------------------


async def test_api_remove_dish_cleans_up_and_leaves_no_dish(aiohttp_client, ctx):
    await planned_ctx(ctx)  # WO 101 uses 1 and 2, DO 102 uses 2 and 3
    ah = ctx.client
    ah.listed[1] += 2  # two of the user's own on top of the add-on's one
    log = ctx.ledger.load(W)
    log.previous[WO.isoformat()] = 99
    ctx.ledger.save(log)
    client = await aiohttp_client(make_app(ctx))
    r = await client.post(f"api/dish/{WO.isoformat()}/remove")
    assert r.status == 200, await r.text()
    assert await r.json() == {"date": WO.isoformat(), "removed": 2, "target": "list",
                              "held": None}
    assert ah.held() == {1: 2, 2: 1, 3: 1}  # the user's own two stay
    log = ctx.ledger.load(W)
    assert log.added == {2: 1, 3: 1} and log.previous == {}
    assert [d["recipe"]["id"] for d in log.plan["dishes"]] == [102]

    body = await (await client.get("api/week")).json()
    wo = body["days"][0]
    assert wo["date"] == WO.isoformat() and wo["geen_gerecht"] is True
    assert wo["recipe"] is None and body["leftovers"] == []
    assert (await client.get(f"api/dish/{WO.isoformat()}")).status == 404
    assert (await client.post(f"api/dish/{WO.isoformat()}/remove")).status == 404
    sensor = [p for path, p in ctx.hass.posts if path == "states/sensor.weekmenu"]
    assert sensor[-1]["attributes"]["geen_gerecht"] == [WO.isoformat()]


async def test_api_persons_4_to_2_lowers(aiohttp_client, ctx, monkeypatch):
    from app.writer import apply
    four = make_plan([dish(WO, 101, [1, 2], 2.0, persons=4)])
    four.basket = {1: 2, 2: 2}  # four persons: two packs each
    await apply(ctx.client, ctx.ledger, ctx.ledger.load(W), four)

    async def fake_rescale(client, plan, day, persons, overrides=None):
        return make_plan([dish(WO, 101, [1, 2], 2.0, persons=persons)])

    monkeypatch.setattr(main, "rescale_dish", fake_rescale)
    client = await aiohttp_client(make_app(ctx))
    r = await client.post(f"api/dish/{WO.isoformat()}/persons", json={"persons": 2})
    assert r.status == 200 and (await r.json())["removed"] == 2
    assert ctx.client.held() == {1: 1, 2: 1} and ctx.ledger.load(W).added == {1: 1, 2: 1}


async def test_api_swap_once_the_week_started_cleans_nothing(aiohttp_client, tmp_path, swaps):
    ctx = make_ctx(tmp_path, now=datetime(2026, 11, 5, 9, 0))
    await planned_ctx(ctx)
    client = await aiohttp_client(make_app(ctx))
    r = await swap(client, WO)
    body = await r.json()
    assert body["removed"] == 0 and ctx.client.held()[2] == 2
    assert {"product": "Product 2", "qty": 1} in body["leftovers"]


async def test_api_week_removed_day_that_is_also_away_shows_once(aiohttp_client, ctx):
    await planned_ctx(ctx)
    client = await aiohttp_client(make_app(ctx))
    assert (await client.post(f"api/dish/{WO.isoformat()}/remove")).status == 200
    ctx.hass.states[helper_persons()["woensdag"]] = "0"
    body = await (await client.get("api/week")).json()
    wo = [d for d in body["days"] if d["date"] == WO.isoformat()]
    assert len(wo) == 1 and wo[0]["geen_gerecht"] is True and wo[0]["niet_thuis"] is False
    assert WO.isoformat() not in body["niet_thuis"]


# --- kookmomenten en restjes (v0.4) ----------------------------------------------------


def leftover_plan():
    wo = dish(WO, 101, [1, 2], 2.0, persons=4)
    wo.leftover_date = DO
    return make_plan([wo, dish(VR, 103, [3], 1.0)])


async def test_api_week_shows_the_leftover_night(aiohttp_client, ctx):
    from app.writer import apply
    await apply(ctx.client, ctx.ledger, ctx.ledger.load(W), leftover_plan())
    client = await aiohttp_client(make_app(ctx))
    body = await (await client.get("api/week")).json()
    assert [d["date"] for d in body["days"]][:3] == [WO.isoformat(), DO.isoformat(),
                                                     VR.isoformat()]
    do = body["days"][1]
    assert do["restjes"] is True and do["recipe"] is None and do["niet_thuis"] is False
    assert do["restjes_van"] == {"date": WO.isoformat(), "title": "Recept 101"}
    assert DO.isoformat() not in body["niet_thuis"]
    d = await (await client.get(f"api/dish/{WO.isoformat()}")).json()
    assert d["restjes"] == DO.isoformat() and d["persons"] == 4


async def test_api_remove_dish_drops_its_leftover_night(aiohttp_client, ctx):
    from app.writer import apply
    await apply(ctx.client, ctx.ledger, ctx.ledger.load(W), leftover_plan())
    client = await aiohttp_client(make_app(ctx))
    assert (await client.post(f"api/dish/{WO.isoformat()}/remove")).status == 200
    body = await (await client.get("api/week")).json()
    days = {d["date"]: d for d in body["days"]}
    assert days[WO.isoformat()]["geen_gerecht"] and days[DO.isoformat()]["geen_gerecht"]
    assert not days[DO.isoformat()].get("restjes")
    sensor = [p for path, p in ctx.hass.posts if path == "states/sensor.weekmenu"][-1]
    assert sensor["attributes"]["geen_gerecht"] == [WO.isoformat(), DO.isoformat()]
    assert all("restjes_van" not in x for x in sensor["attributes"]["dishes"])


async def test_api_settings_bounds_cooking_moments_to_eat_days(aiohttp_client, ctx):
    for dag in ("zaterdag", "zondag", "maandag", "dinsdag"):
        ctx.hass.states[helper_persons()[dag]] = "0"
    ctx.hass.states[HELPER_AVONDEN] = "5"
    client = await aiohttp_client(make_app(ctx))
    s = await (await client.get("api/settings")).json()
    assert s["avonden"] == 3 and s["eetdagen"] == 3
    r = await client.post("api/settings", json={"entity_id": HELPER_AVONDEN, "value": 6})
    assert r.status == 200 and (await r.json())["value"] == 3
    assert ctx.hass.posts[-1] == (
        "services/input_number/set_value", {"entity_id": HELPER_AVONDEN, "value": 3})
    r = await client.post("api/settings", json={"entity_id": HELPER_AVONDEN, "value": "x"})
    assert r.status == 400


# --- fix round 1 (v0.4 B) ---------------------------------------------------------------


async def test_api_persons_up_to_16_for_a_dish_with_leftovers(aiohttp_client, ctx, monkeypatch):
    from app.writer import apply
    await apply(ctx.client, ctx.ledger, ctx.ledger.load(W), leftover_plan())
    seen = []

    async def fake_rescale(client, plan, day, persons, overrides=None):
        seen.append((day, persons))
        out = []
        for d in plan.dishes:
            if d.date == day:
                d = dish(d.date, d.recipe.id, [ln.product.id for ln in d.lines], d.savings,
                         persons=persons)
                d.leftover_date = DO if day == WO else None
            out.append(d)
        return make_plan(out)

    monkeypatch.setattr(main, "rescale_dish", fake_rescale)
    client = await aiohttp_client(make_app(ctx))
    r = await client.post(f"api/dish/{WO.isoformat()}/persons", json={"persons": 10})
    assert r.status == 200, await r.text()
    body = await r.json()
    assert body["persons"] == 10 and body["restjes"] == DO.isoformat()  # the note stays
    r = await client.post(f"api/dish/{WO.isoformat()}/persons", json={"persons": 9})
    assert r.status == 200  # the minus button works from 10
    assert (await client.post(f"api/dish/{WO.isoformat()}/persons",
                              json={"persons": 17})).status == 400
    # a dish without leftovers stays at 8
    assert (await client.post(f"api/dish/{VR.isoformat()}/persons",
                              json={"persons": 9})).status == 400
    assert seen == [(WO, 10), (WO, 9)]


async def test_niet_thuis_tap_lowers_the_cooking_moments_helper(aiohttp_client, ctx):
    for dag in ("zaterdag", "zondag", "maandag"):
        ctx.hass.states[helper_persons()[dag]] = "0"
    ctx.hass.states[HELPER_AVONDEN] = "4"  # 4 eat days: in bounds
    client = await aiohttp_client(make_app(ctx))
    r = await client.post("api/settings", json={"entity_id": helper_persons()["dinsdag"],
                                                "away": True})
    assert r.status == 200
    assert ("services/input_number/set_value",
            {"entity_id": HELPER_AVONDEN, "value": 3}) in ctx.hass.posts


async def test_api_week_shows_eat_days_without_a_cooking_moment(aiohttp_client, ctx):
    from app.writer import apply
    await apply(ctx.client, ctx.ledger, ctx.ledger.load(W), leftover_plan())
    for dag in ("zondag", "maandag", "dinsdag"):
        ctx.hass.states[helper_persons()[dag]] = "0"
    client = await aiohttp_client(make_app(ctx))
    body = await (await client.get("api/week")).json()
    za = [d for d in body["days"] if d["date"] == "2026-11-07"]
    # v1.1: a cook evening of the helpers without a dish can still be filled ("kies")
    assert za == [{"date": "2026-11-07", "dag": "zaterdag", "niet_thuis": False,
                   "geen_kookmoment": True, "persons": 0, "recipe": None, "savings": 0.0,
                   "kies": True}]
    assert [d["date"] for d in body["days"]][-3:] == ["2026-11-08", "2026-11-09", "2026-11-10"]
    assert all(d["niet_thuis"] for d in body["days"][-3:])


async def test_api_status_running_is_a_menu_round_not_the_lock(aiohttp_client, ctx,
                                                               monkeypatch):
    """The hourly new-folder look holds the lock: no "Er wordt een menu gemaakt" and no
    "Er loopt al een ronde" for that. `rounds` lets the app see that a round ended."""
    client = await aiohttp_client(make_app(ctx))
    async with ctx.lock:  # like check_new_folder
        status = await (await client.get("api/status")).json()
        assert status["running"] is False and status["rounds"] == 0
    with_new_folder(ctx)
    ctx.in_round = True
    assert (await (await client.get("api/status")).json())["running"] is True
    assert (await client.post("api/run", json={"mode": "bijwerken"})).status == 409
    ctx.in_round = False

    async def fake_round(c, now, mode):
        c.rounds += 1

    monkeypatch.setattr(main, "run_round", fake_round)
    async with ctx.lock:
        assert (await client.post("api/run", json={"mode": "bijwerken"})).status == 202
    await asyncio.gather(*ctx.tasks)
    assert (await (await client.get("api/status")).json())["rounds"] == 1


# --- Ruling 42: never into an order of another week ---------------------------------

HELD_W = ("Het menu staat klaar; de producten worden toegevoegd zodra er een "
          "bestelling of lijst voor woensdag 4 november is")


async def test_api_swap_writes_nothing_into_an_order_of_another_week(aiohttp_client, ctx,
                                                                     swaps):
    from app.ahclient import ActiveOrder
    await planned_ctx(ctx)  # the menu is on the list
    ah = ctx.client
    ah.order = ActiveOrder(5, {7: 1}, date(2026, 10, 28))  # last week's order, still open
    before = len(ah.writes)
    client = await aiohttp_client(make_app(ctx))
    r = await swap(client, WO)
    assert r.status == 200, await r.text()
    body = await r.json()
    assert body["recipe"]["id"] == 301 and body["held"] == HELD_W and body["removed"] == 0
    assert len(ah.writes) == before and ah.order.items == {7: 1}
    log = ctx.ledger.load(W)
    assert log.held and {d["recipe"]["id"] for d in log.plan["dishes"]} == {301, 102}
    week = await (await client.get("api/week")).json()
    assert week["held"] == HELD_W

    ah.order = None  # delivered: the next write goes on the list
    r = await client.post(f"api/dish/{WO.isoformat()}/back")
    body = await r.json()
    assert body["held"] is None
    assert (await (await client.get("api/week")).json())["held"] is None


async def test_api_swap_then_back_never_rebuys_what_the_user_removed(aiohttp_client, ctx,
                                                                     swaps):
    """Ruling 43: 101 needs 1 and 2, 102 needs 2 and 3; the user takes both 2s off."""
    await planned_ctx(ctx)
    ah = ctx.client
    ah.listed.pop(2)
    n = len(ah.writes)
    client = await aiohttp_client(make_app(ctx))
    r = await swap(client, WO)  # 301: 1 and 9
    assert r.status == 200, await r.text()
    assert ctx.ledger.load(W).removed_by_user == {2: 1}
    r = await client.post(f"api/dish/{WO.isoformat()}/back")
    assert r.status == 200, await r.text()
    assert ah.held() == {1: 1, 3: 1}  # 9 went off again, 2 never came back
    assert [w for w in ah.writes[n:] if w[1].get(2, 0) > 0] == []  # 2 was never added


async def test_api_swap_in_the_running_week_names_what_to_add_by_hand(aiohttp_client,
                                                                      tmp_path, swaps):
    """Ruling 44: Thursday 5 November, next week's order is open. The running week is
    never retried, so the note names the products instead of promising."""
    from app.ahclient import ActiveOrder
    ctx = make_ctx(tmp_path, now=datetime(2026, 11, 5, 10, 0))
    await planned_ctx(ctx)
    ctx.client.order = ActiveOrder(5, {}, date(2026, 11, 11))
    client = await aiohttp_client(make_app(ctx))
    body = await (await swap(client, WO)).json()
    manual = ("Deze producten konden niet in je bestelling. Voeg ze zelf toe in de "
              "AH-app: Product 9")
    assert body["held"] == manual and ctx.client.order.items == {}
    week = await (await client.get(f"api/week?week={W.isoformat()}")).json()
    assert week["held"] == manual


async def test_stale_held_push_never_overwrites_a_web_write(aiohttp_client, tmp_path):
    """Round 3: the push for a held week holds ctx.lock from load to save, so a
    "Gerecht weghalen" on that week waits and is kept."""
    from app.ahclient import ActiveOrder
    wk = date(2026, 11, 11)
    ctx = make_ctx(tmp_path, now=datetime(2026, 11, 9, 10, 0))  # target: the 18th
    ctx.client.order = ActiveOrder(5, {}, date(2026, 10, 28))
    held_plan = make_plan([dish(wk, 101, [1, 2], 2.0), dish(date(2026, 11, 12), 102, [3], 1.0)])
    ctx.ledger.save(WeekLog(wk, plan=held_plan.to_dict(), held="bestelling elders",
                            target="order"))
    started, release = asyncio.Event(), asyncio.Event()

    async def slow_notify(title, message, url=None):
        started.set()
        await release.wait()
        return True

    ctx.hass.notify = slow_notify
    client = await aiohttp_client(make_app(ctx))
    push = asyncio.create_task(main.notify_stale_held(ctx, date(2026, 11, 18)))
    await started.wait()
    remove = asyncio.create_task(client.post(f"api/dish/{wk.isoformat()}/remove"))
    await asyncio.sleep(0.05)
    assert not remove.done()  # waits for the lock
    release.set()
    await push
    r = await remove
    assert r.status == 200, await r.text()
    log = ctx.ledger.load(wk)
    assert [d["recipe"]["id"] for d in log.plan["dishes"]] == [102]  # the write is kept
    # the push was saved first (products 1, 2, 3); the removal then left only 3
    # missing, another set, so the marker is reset for one new push
    assert log.held_notified is False and log.held_notified_items == [1, 2, 3]
    assert ctx.client.writes == []


# --- v0.6 -------------------------------------------------------------------------------


async def test_api_write_to_a_too_new_week_is_refused_with_the_update_message(
        aiohttp_client, ctx, swaps):
    import json as _json
    from app.durable import UPDATE_MESSAGE
    await planned_ctx(ctx)
    path = ctx.ledger.root / f"{W.isoformat()}.json"
    path.write_text(_json.dumps({**_json.loads(path.read_text(encoding="utf-8")),
                                 "schema": 9}), encoding="utf-8")
    before, writes = path.read_bytes(), list(ctx.client.writes)
    client = await aiohttp_client(make_app(ctx))
    r = await swap(client, WO)
    assert r.status == 409 and (await r.json())["error"] == UPDATE_MESSAGE
    assert ctx.client.writes == writes and path.read_bytes() == before
    assert f"{W.isoformat()}.json" in ctx.too_new
    r = await client.get(f"api/dish/{WO.isoformat()}")  # showing it still works
    assert r.status == 200
