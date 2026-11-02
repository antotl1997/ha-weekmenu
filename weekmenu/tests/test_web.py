import asyncio
import logging
from datetime import date, datetime

import pytest

from app import main
from app.ahclient import AhApiError
from app.hass import HELPER_AVONDEN, helper_persons
from app.ledger import WeekLog
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
    """swap_dish/restore_dish without Allerhande: fixed replacement dishes."""
    entered = []

    async def fake_swap(client, plan, day, bonus, s, exclude):
        entered.append(day)
        await asyncio.sleep(0.02)
        new = {WO: dish(WO, 301, [1, 9], 1.5), DO: dish(DO, 302, [2, 9], 1.5)}[day]
        assert new.recipe.id not in exclude
        return make_plan([d for d in plan.dishes if d.date != day] + [new])

    async def fake_restore(client, plan, day, recipe_id, bonus):
        old = {101: dish(WO, 101, [1, 2], 2.0), 102: dish(DO, 102, [2, 3], 3.0)}[recipe_id]
        return make_plan([d for d in plan.dishes if d.date != day] + [old])

    monkeypatch.setattr(main, "swap_dish", fake_swap)
    monkeypatch.setattr(main, "restore_dish", fake_restore)
    return entered


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


async def test_api_swap_then_back_adds_nothing_twice(aiohttp_client, ctx, swaps):
    await planned_ctx(ctx)
    ah = ctx.client
    assert ah.held() == {1: 1, 2: 2, 3: 1}
    client = await aiohttp_client(make_app(ctx))

    r = await client.post(f"api/dish/{WO.isoformat()}/swap")
    assert r.status == 200, await r.text()
    body = await r.json()
    assert body["recipe"]["id"] == 301
    assert ah.held() == {1: 1, 2: 2, 3: 1, 9: 1}
    log = ctx.ledger.load(W)
    assert log.swapped_out[WO.isoformat()] == [101] and log.previous[WO.isoformat()] == 101
    assert {"product": "Product 2", "qty": 1} in body["leftovers"]

    r = await client.post(f"api/dish/{WO.isoformat()}/back")
    assert r.status == 200, await r.text()
    assert (await r.json())["recipe"]["id"] == 101
    assert ah.held() == {1: 1, 2: 2, 3: 1, 9: 1}  # nothing bought twice
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
        client.post(f"api/dish/{WO.isoformat()}/swap"),
        client.post(f"api/dish/{DO.isoformat()}/swap"),
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
    ctx.hass.states["input_select.weekmenu_ouders"] = "vrijdag"
    client = await aiohttp_client(make_app(ctx))
    body = await (await client.get("api/week")).json()
    assert body["week"] == W.isoformat() and body["target"] == "list"
    assert body["savings"] == 5.0 and body["products"] == 3 and body["bonus_products"] == 3
    days = body["days"]
    assert [d["date"] for d in days] == ["2026-11-04", "2026-11-05", "2026-11-06"]
    assert days[0]["dag"] == "woensdag" and days[0]["recipe"]["title"] == "Recept 101"
    assert set(days[0]["recipe"]) == {"id", "title", "image", "cook", "oven", "wait",
                                      "cuisine", "url"}
    assert days[2]["ouders"] is True and days[2]["recipe"] is None

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
    assert body["week"] == "2026-11-11" and body["days"] == []  # nothing yet: next week
    await planned_ctx(ctx)
    body = await (await client.get("api/week")).json()
    assert body["week"] == W.isoformat()


async def test_api_persons_rescales_only_that_dish(aiohttp_client, ctx, monkeypatch):
    await planned_ctx(ctx)
    seen = []

    async def fake_rescale(client, plan, day, persons):
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
    assert (await client.post(f"api/dish/{WO.isoformat()}/swap")).status == 200
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
    client = await aiohttp_client(make_app(ctx))
    assert (await client.post("api/run", json={"mode": "fout"})).status == 400
    r = await client.post("api/run", json={"mode": "nu_maken"})
    assert r.status == 202
    r = await client.post("api/run", json={"mode": "nieuw_menu"})
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
    assert {"week": "2026-10-28", "savings": 4.0, "counted": True} in body["weeks"]


async def test_api_login_expired_is_reported(aiohttp_client, ctx, swaps):
    from app.ahclient import AhLoginExpired
    await planned_ctx(ctx)
    ctx.client.raise_on["bonus_periods"] = AhLoginExpired("login expired", 401)
    client = await aiohttp_client(make_app(ctx))
    r = await client.post(f"api/dish/{WO.isoformat()}/swap")
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
    assert (await client.post(f"api/dish/{WO.isoformat()}/swap")).status == 200
    ah = ctx.client
    reads, writes = list(ah.calls), list(ah.writes)

    r = await client.post(f"api/dish/{WO.isoformat()}/back")
    assert r.status == 200, await r.text()
    after = await r.json()
    for key in ("recipe", "lines", "persons", "steps", "not_found", "savings"):
        assert after[key] == before[key], key
    assert after["recipe"]["oven"] == 20 and after["recipe"]["wait"] == 60
    assert ah.calls == reads  # no bonus, recipe or product reads
    assert ah.writes == writes  # everything was already bought
    assert ctx.ledger.load(W).previous_dish == {}


async def test_api_back_falls_back_for_logs_without_saved_dish(aiohttp_client, ctx, swaps):
    await planned_ctx(ctx)
    client = await aiohttp_client(make_app(ctx))
    assert (await client.post(f"api/dish/{WO.isoformat()}/swap")).status == 200
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


async def test_api_run_before_folder_waits_and_says_when(aiohttp_client, tmp_path):
    from app.ahclient import BonusPeriod
    cur = BonusPeriod(date(2026, 10, 26), date(2026, 11, 1), date(2026, 10, 30), ["Vlees"])
    ah = FakeAh(tmp_path, periods=[cur])
    ctx = make_ctx(tmp_path, ah=ah, now=datetime(2026, 10, 29, 10, 0))
    client = await aiohttp_client(make_app(ctx))
    status = await (await client.get("api/status")).json()
    assert status["waiting_for_folder"] is None
    assert (await client.post("api/run", json={"mode": "nu_maken"})).status == 202
    await asyncio.gather(*ctx.tasks)
    assert ah.writes == [] and "bonus_offers" not in ah.calls
    assert ctx.hass.notifications() == []
    status = await (await client.get("api/status")).json()
    assert status["waiting_for_folder"] == {"week": "2026-11-04", "expected": "2026-10-30"}
    assert status["error"] is None and status["running"] is False
    body = await (await client.get("api/week")).json()
    assert body["week"] == "2026-11-04" and body["days"] == []
    assert body["bestelling"] == "Voor de bestelling van woensdag 4 november"
    assert body["waiting_for_folder"] == {"expected": "2026-10-30"}


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
    assert all(d["recipe"] is None and d["persons"] == 0 and d["ouders"] is False for d in away)
    assert all(d["niet_thuis"] is False for d in body["days"] if d["recipe"])


async def test_api_week_without_a_menu_says_when_nobody_eats_at_home(aiohttp_client, ctx):
    for eid in helper_persons().values():
        ctx.hass.states[eid] = "0"
    client = await aiohttp_client(make_app(ctx))
    body = await (await client.get("api/week")).json()
    assert body["days"] == [] and body["geen_avonden"] is True
    assert len(body["niet_thuis"]) == 7


async def test_api_settings_accepts_zero_persons(aiohttp_client, ctx):
    client = await aiohttp_client(make_app(ctx))
    eid = helper_persons()["vrijdag"]
    r = await client.post("api/settings", json={"entity_id": eid, "value": 0})
    assert r.status == 200
    assert ctx.hass.posts[-1] == ("services/input_number/set_value", {"entity_id": eid, "value": 0})


async def test_api_week_niet_thuis_skips_parents_day_and_days_with_a_dish(aiohttp_client, ctx):
    await planned_ctx(ctx)  # dishes on wo, do (and one more)
    ctx.hass.states["input_select.weekmenu_ouders"] = "vrijdag"
    ctx.hass.states["input_number.weekmenu_personen_vrijdag"] = "0"
    ctx.hass.states["input_number.weekmenu_personen_donderdag"] = "0"  # still has a dish
    ctx.hass.states["input_number.weekmenu_personen_zondag"] = "0"
    client = await aiohttp_client(make_app(ctx))
    body = await (await client.get("api/week")).json()
    dish_days = {d["date"] for d in body["days"] if d["recipe"]}
    assert "2026-11-05" in dish_days
    assert body["niet_thuis"] == ["2026-11-08"]
    ouders = [d for d in body["days"] if d["ouders"]]
    assert [d["date"] for d in ouders] == ["2026-11-06"] and ouders[0]["niet_thuis"] is False

