"""v0.7: writes paused when AH changed, the one push, diagnose, health, logout."""
import asyncio
import json
import logging
import time
from datetime import date, datetime
from pathlib import Path

import httpx
import pytest

from app import ahclient, main
from app.ahclient import AhClient, Product
from app.diag import install_ring
from app.durable import UPDATE_MESSAGE
from app.health import PAUSED_MESSAGE, PAUSED_NOTE, ApiHealth
from app.ledger import Ledger, WeekLog
from app.main import Context
from app.tokens import Tokens, TokenStore
from app.web import make_app
from app.writer import apply, apply_extras
from tests.fakes import SUNDAY, W, FakeAh, FakeHass, dish, make_ctx, make_plan

FIX = Path(__file__).parent / "fixtures"
BASE = "https://api.ah.nl"
META = BASE + "/mobile-services/bonuspage/v3/metadata"
LIST_URL = BASE + "/mobile-services/shoppinglist/v2/items"
ORDER_GET = BASE + "/mobile-services/order/v1/summaries/active"
ACCESS, REFRESH = "tok-A1b2C3d4E5f6G7h8", "ref-Z9y8X7w6V5u4T3s2"


@pytest.fixture(autouse=True)
def no_delay(monkeypatch):
    monkeypatch.setattr(ahclient, "CALL_DELAY", 0)


def fx(name):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


def paused(ctx, by="lijst"):
    ctx.health.failed(by, "vorm")
    ctx.health.failed(by, "vorm")
    assert ctx.health.writes_paused


def plan_ab():
    return make_plan([dish(W, 101, [1, 2], 2.0)])


# --- "Klaar als": a changed field gives exactly one message and no write ----------------


def real_ctx(tmp_path, http, hass=None):
    store = TokenStore(tmp_path / "tokens.json")
    store.save(Tokens(ACCESS, REFRESH, time.time() + 3600))
    ctx = Context(client=AhClient(store, http), ledger=Ledger(tmp_path / "weeks"),
                  hass=hass or FakeHass(tmp_path / "totaal.json", enabled=False),
                  store=store, pantry=["olie"], total_path=tmp_path / "totaal.json",
                  state_path=tmp_path / "state.json")
    ctx.clock = lambda: SUNDAY
    main.load_state(ctx)
    return ctx


async def test_a_changed_list_field_gives_one_message_and_no_write(
        respx_mock, tmp_path, aiohttp_client):
    broken = fx("handmade-shoppinglist__items.json")["body"]
    broken["entries"] = broken.pop("items")  # AH renamed a field
    good = fx("handmade-shoppinglist__items.json")["body"]
    absent = fx("order-active__default.json")
    respx_mock.get(META).respond(json=fx("bonus-metadata__v3.json"))
    respx_mock.get(ORDER_GET).respond(absent["status"], json=absent["body"])
    lst = respx_mock.get(LIST_URL).respond(json=broken)
    patch = respx_mock.patch(LIST_URL).respond(200)
    put = respx_mock.put(BASE + "/mobile-services/order/v1/items").respond(200)
    http = httpx.AsyncClient()
    ctx = real_ctx(tmp_path, http)
    ctx.bonus_cache = {date(2026, 11, 2): [
        Product(55, "AH Kipfilet", None, 6.0, 4.0, True, "25% korting", True, "Vlees")]}
    ctx.ledger.save(WeekLog(W))
    client = await aiohttp_client(make_app(ctx))
    hass = ctx.hass

    for _ in range(2):  # two taps, two shape errors on the list: writing pauses
        r = await client.post(f"api/offers/55?week={W.isoformat()}")
        assert r.status == 502
    assert ctx.health.writes_paused
    assert [n["message"] for n in hass.notifications()] == [PAUSED_MESSAGE]

    r = await client.post(f"api/offers/55?week={W.isoformat()}")  # paused: no AH call
    assert r.status == 409 and (await r.json())["error"] == PAUSED_NOTE
    reads = lst.call_count
    await main.tick(ctx, datetime(2026, 11, 1, 9, 0))  # read round: the list still breaks
    assert lst.call_count == reads + 1 and ctx.health.writes_paused
    week = await (await client.get(f"api/week?week={W.isoformat()}")).json()
    assert week["writes_paused"] == PAUSED_NOTE

    # a restart: the pause and the sent mark come back from state.json
    again = real_ctx(tmp_path, http, hass)
    assert again.health.writes_paused and again.health_notified
    await main.tick(again, datetime(2026, 11, 1, 10, 0))
    assert len(hass.notifications()) == 1  # exactly one message, over restarts too
    assert patch.call_count == 0 and put.call_count == 0  # and not one write

    # AH fixed it: the hourly read round switches writing on again, the mark is cleared
    lst.respond(json=good)
    await main.tick(again, datetime(2026, 11, 1, 11, 0))
    assert not again.health.writes_paused and not again.health_notified
    state = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert state["api"]["writes_paused"] is False and state["api"]["notified"] is False
    await http.aclose()


# --- the writer while paused ------------------------------------------------------------


async def test_apply_while_paused_calls_nothing_at_ah_and_keeps_the_round_open(tmp_path):
    ctx = make_ctx(tmp_path)
    ah = ctx.client
    wlog = WeekLog(W)
    wlog.round_done = wlog.completion_tried = True
    paused(ctx)
    res = await apply(ah, ctx.ledger, wlog, plan_ab())
    assert res.held == PAUSED_NOTE and not res.added
    assert ah.writes == [] and ah.calls == [] and ah.held() == {}
    saved = ctx.ledger.load(W)
    assert saved.plan and not saved.round_done and not saved.completion_tried
    assert saved.held == PAUSED_NOTE  # v1.0 final review I1: a week left over gets its push


async def test_apply_while_paused_with_nothing_to_write_has_no_note(tmp_path):
    ctx = make_ctx(tmp_path)
    wlog = WeekLog(W)
    wlog.added = {1: 1, 2: 1}
    wlog.round_done = True
    paused(ctx)
    res = await apply(ctx.client, ctx.ledger, wlog, plan_ab())
    assert res.held is None and ctx.ledger.load(W).round_done


async def test_apply_extras_while_paused_writes_nothing(tmp_path):
    ctx = make_ctx(tmp_path)
    paused(ctx, "bestelling")
    res = await apply_extras(ctx.client, ctx.ledger, WeekLog(W), {7: 1}, {7: "Koffie"})
    assert res.held == PAUSED_NOTE and ctx.client.writes == []


async def test_writing_on_again_writes_what_the_paused_round_kept(tmp_path):
    ctx = make_ctx(tmp_path)
    paused(ctx)
    await apply(ctx.client, ctx.ledger, WeekLog(W), plan_ab())
    ctx.health.resume("test")
    res = await apply(ctx.client, ctx.ledger, ctx.ledger.load(W), plan_ab())
    assert res.held is None and ctx.client.held() == {1: 1, 2: 1}


async def test_a_round_while_paused_keeps_the_menu_and_says_why(tmp_path):
    ctx = make_ctx(tmp_path)
    ctx.ledger.save(WeekLog(W))
    wlog = ctx.ledger.load(W)
    wlog.plan = plan_ab().to_dict()
    ctx.ledger.save(wlog)
    paused(ctx)
    out = await main.run_round(ctx, SUNDAY, "nu_maken")
    assert out.status == "done" and out.result.held == PAUSED_NOTE
    assert ctx.client.writes == []
    msgs = [n["message"] for n in ctx.hass.notifications()]
    assert msgs == [PAUSED_MESSAGE]  # the same menu, nothing new: only the pause push
    assert not ctx.ledger.load(W).round_done  # the hourly look completes it later


async def test_a_failing_round_while_paused_sends_no_second_failure_message(tmp_path):
    ah = FakeAh(tmp_path)
    ctx = make_ctx(tmp_path, ah=ah)
    wlog = WeekLog(W)
    wlog.plan = plan_ab().to_dict()
    ctx.ledger.save(wlog)
    paused(ctx)
    ctx.health_notified = True
    ah.raise_on["get_recipe"] = ahclient.AhShapeError("recept: onverwachte vorm", 200)
    ah.raise_on["bonus_periods"] = ahclient.AhShapeError("bonus: vorm", 200)
    out = await main.run_round(ctx, SUNDAY, "nieuw_menu")
    assert out.status == "error"
    assert ctx.hass.notifications() == []


async def test_sync_health_retries_a_failed_push_and_saves_the_pause(tmp_path):
    hass = FakeHass(tmp_path / "totaal.json", fail_posts=True)
    ctx = make_ctx(tmp_path, hass=hass)
    paused(ctx)
    await main.sync_health(ctx)
    assert not ctx.health_notified  # HA was away: try again next time
    state = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert state["api"]["writes_paused"] is True and state["api"]["paused_by"] == "lijst"
    hass.fail_posts = False
    await main.sync_health(ctx)
    await main.sync_health(ctx)
    assert ctx.health_notified and len(hass.notifications()) == 2  # one failed, one sent


# --- the "werk de add-on bij" message: once, also over a restart -------------------------


async def test_update_message_goes_once_over_a_restart(tmp_path):
    ctx = make_ctx(tmp_path)
    ctx.too_new.add("totaal.json")
    await main.notify_too_new(ctx)
    assert len(ctx.hass.notifications()) == 1
    again = make_ctx(tmp_path, hass=ctx.hass)
    main.load_state(again)
    again.too_new.add("totaal.json")
    await main.notify_too_new(again)
    assert len(ctx.hass.notifications()) == 1


async def test_update_mark_is_cleared_once_nothing_is_newer(tmp_path):
    ctx = make_ctx(tmp_path)
    ctx.too_new_notified = True
    main._save_state(ctx)
    again = make_ctx(tmp_path)
    main.load_state(again)
    assert again.too_new_notified
    main.scan_too_new(again)
    assert not again.too_new_notified
    third = make_ctx(tmp_path)
    main.load_state(third)
    assert not third.too_new_notified


def test_scan_finds_a_newer_week_log(tmp_path):
    ctx = make_ctx(tmp_path)
    (tmp_path / "weeks").mkdir(exist_ok=True)
    (tmp_path / "weeks" / f"{W.isoformat()}.json").write_text(
        json.dumps({"schema": 99, "week": W.isoformat()}), encoding="utf-8")
    main.scan_too_new(ctx)
    assert f"{W.isoformat()}.json" in ctx.too_new


# --- options ----------------------------------------------------------------------------


@pytest.mark.parametrize("opts,want", [
    ({"ah_app_version": "9.31"}, "9.31"),
    ({"ah_app_version": "  9.29.1 "}, "9.29.1"),
    ({"ah_app_version": "9.x"}, "9.28"),
    ({}, "9.28"),
])
def test_app_version_option(tmp_path, opts, want):
    path = tmp_path / "options.json"
    path.write_text(json.dumps(opts), encoding="utf-8")
    assert main.load_app_version(path) == want


def test_app_version_option_missing_file(tmp_path):
    assert main.load_app_version(tmp_path / "nope.json") == "9.28"


# --- web: status, health, logout, diagnose ------------------------------------------------


async def test_status_names_the_app_version_and_the_last_ah_sync(aiohttp_client, tmp_path):
    ctx = make_ctx(tmp_path, app_version="9.31")
    ctx.health.ok("bonus")
    body = await (await (await aiohttp_client(make_app(ctx))).get("api/status")).json()
    assert body["ah_app_version"] == "9.31"
    assert body["last_ah_sync"] == ctx.health.last_ok and body["last_ah_sync"]
    assert body["update_needed"] is None and body["writes_paused"] is False


async def test_newer_tokens_show_the_update_message_not_the_login(aiohttp_client, tmp_path):
    ctx = make_ctx(tmp_path)
    (tmp_path / "tokens.json").write_text(json.dumps({"schema": 9}), encoding="utf-8")
    client = await aiohttp_client(make_app(ctx))
    body = await (await client.get("api/status")).json()
    assert body["update_needed"] == UPDATE_MESSAGE and body["logged_in"] is False
    r = await client.post("api/login", json={"code": "Ab12-cD34"})
    assert r.status == 409 and (await r.json())["error"] == UPDATE_MESSAGE
    assert ctx.client.codes == []  # the code is not spent


async def test_health_answers_without_the_lock_and_without_ah(aiohttp_client, tmp_path):
    ctx = make_ctx(tmp_path)
    client = await aiohttp_client(make_app(ctx))
    async with ctx.lock:  # a long round holds the lock
        r = await asyncio.wait_for(client.get("api/health"), 2)
        assert r.status == 200 and await r.json() == {"ok": True}
    assert ctx.client.calls == [] and ctx.client.fresh_checks == 0


async def test_health_follows_the_supervisor_allowlist(aiohttp_client, tmp_path):
    ctx = make_ctx(tmp_path)
    # the test client comes from 127.0.0.1: allowed when that is the Supervisor address
    ok = await aiohttp_client(make_app(ctx, allowed_remotes={"127.0.0.1"}))
    assert (await ok.get("api/health")).status == 200
    other = await aiohttp_client(make_app(ctx, allowed_remotes={main.INGRESS_PROXY}))
    assert (await other.get("api/health")).status == 403


async def test_logout_removes_the_tokens_and_shows_the_login(aiohttp_client, tmp_path):
    ctx = make_ctx(tmp_path)
    ctx.store.save(Tokens(ACCESS, REFRESH, 9e9))
    (tmp_path / "tokens.json.bak").write_text("{}", encoding="utf-8")
    client = await aiohttp_client(make_app(ctx))
    assert (await (await client.get("api/status")).json())["logged_in"] is True
    r = await client.post("api/logout")
    assert r.status == 200
    assert not (tmp_path / "tokens.json").exists()
    assert not (tmp_path / "tokens.json.bak").exists()
    assert ctx.login_expired
    assert (await (await client.get("api/status")).json())["logged_in"] is False


async def test_diagnose_has_everything_and_no_secret(aiohttp_client, tmp_path):
    ctx = make_ctx(tmp_path, app_version="9.30")
    ctx.store.save(Tokens(ACCESS, REFRESH, 9e9))
    ctx.planner_seconds = 1.25
    wlog = WeekLog(W)
    wlog.added = {1: 2}
    wlog.pending = {3: {"before": 0, "delta": 1, "target": "list", "order_id": None}}
    wlog.round_done = True
    ctx.ledger.save(wlog)
    ctx.health.failed("bestelling", "HTTP 403")
    install_ring()
    lg = logging.getLogger("weekmenu.test")
    lg.warning("een regel met %s en Bearer %s en appie://login-exit?code=Ab12-cD34",
               ACCESS, REFRESH)
    client = await aiohttp_client(make_app(ctx))
    r = await client.get("api/diagnose")
    assert r.status == 200
    text = await r.text()
    body = json.loads(text)
    assert body["addon_version"] and body["ah_app_version"] == "9.30"
    assert body["writes_paused"] is False
    assert body["api_health"]["endpoints"]["bestelling"]["laatste_fout"] == "HTTP 403"
    assert body["planner_seconds"] == 1.25
    assert body["schemas"]["code"]["tokens.json"] == 1
    assert {"week": W.isoformat(), "added": 1, "pending": 1, "unknown": 0, "held": False,
            "round_done": True, "too_new": False} in body["weeks"]
    assert any("een regel met" in line for line in body["log"])
    assert len(body["log"]) <= 50
    for secret in (ACCESS, REFRESH, "Ab12-cD34", "Authorization"):
        assert secret not in text
    assert ctx.client.calls == []  # no AH call


async def test_retry_switches_writing_on_and_clears_the_mark(aiohttp_client, tmp_path):
    ctx = make_ctx(tmp_path)
    client = await aiohttp_client(make_app(ctx))
    paused(ctx)
    await client.get("api/status")  # any request: the one push goes out
    assert ctx.health_notified and len(ctx.hass.notifications()) == 1
    r = await client.post("api/diagnose/retry")
    assert (await r.json()) == {"writes_paused": False}
    assert not ctx.health.writes_paused and not ctx.health_notified
    state = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert state["api"] == {"writes_paused": False, "paused_by": None, "paused_at": None,
                            "notified": False, "last_ok": None}


async def test_context_shares_one_health_object_with_the_client(tmp_path):
    h = ApiHealth()
    store = TokenStore(tmp_path / "tokens.json")
    async with httpx.AsyncClient() as http:
        c = Context(client=AhClient(store, http, health=h), ledger=Ledger(tmp_path / "w"),
                    hass=None, store=store, pantry=[])
        assert c.health is h
    fake = FakeAh(tmp_path)
    c2 = make_ctx(tmp_path, ah=fake)
    assert fake.health is c2.health


# --- fix round 1 (Ruling 48) ---------------------------------------------------------------


async def test_refused_writes_pause_with_one_push(respx_mock, tmp_path):
    respx_mock.get(LIST_URL).respond(json=fx("handmade-shoppinglist__items.json")["body"])
    absent = fx("order-active__default.json")
    respx_mock.get(ORDER_GET).respond(absent["status"], json=absent["body"])
    patch = respx_mock.patch(LIST_URL).respond(400)
    http = httpx.AsyncClient()
    ctx = real_ctx(tmp_path, http)
    for pid in (7, 8):  # two taps on two staples
        with pytest.raises(ahclient.AhApiError):
            await apply_extras(ctx.client, ctx.ledger, ctx.ledger.load(W), {pid: 1})
        await main.sync_health(ctx)
    assert ctx.health.writes_paused and patch.call_count == 2
    res = await apply_extras(ctx.client, ctx.ledger, ctx.ledger.load(W), {9: 1})
    assert res.held == PAUSED_NOTE and patch.call_count == 2
    await main.sync_health(ctx)
    assert [n["message"] for n in ctx.hass.notifications()] == [PAUSED_MESSAGE]
    await http.aclose()


async def test_two_concurrent_syncs_send_one_push(tmp_path):
    hass = FakeHass(tmp_path / "totaal.json")
    real_post = hass._post

    async def slow_post(path, payload):
        await asyncio.sleep(0.05)
        await real_post(path, payload)

    hass._post = slow_post
    ctx = make_ctx(tmp_path, hass=hass)
    paused(ctx)
    await asyncio.gather(main.sync_health(ctx), main.sync_health(ctx))
    assert len(hass.notifications()) == 1


def test_the_ring_keeps_only_the_add_ons_own_lines_and_scrubs_ingress_paths():
    from app.diag import RingHandler, scrub
    ring = RingHandler()
    for name in ("aiohttp.access", "httpx", "httpcore.http11", "weekmenu.web", "app.planner"):
        rec = logging.LogRecord(name, logging.INFO, __file__, 1,
                                '172.30.32.2 "GET /api/week" 200 "https://ha.local/api/'
                                'hassio_ingress/AbCdEfGh123/" from %s', (name,), None)
        ring.handle(rec)
    assert [line.rsplit(" ", 1)[-1] for line in ring.lines] == ["weekmenu.web", "app.planner"]
    assert "AbCdEfGh123" not in scrub("x /api/hassio_ingress/AbCdEfGh123/api/week", [])


async def test_diagnose_drops_an_access_log_line_with_an_ingress_referer(aiohttp_client, tmp_path):
    ctx = make_ctx(tmp_path)
    install_ring()
    logging.getLogger("aiohttp.access").warning(
        '172.30.32.2 "GET /api/week HTTP/1.1" 200 "https://ha.local/api/hassio_ingress/'
        'Ingr3ssT0ken42/" "Mozilla"')
    logging.getLogger("weekmenu.test").warning("pad /api/hassio_ingress/Ingr3ssT0ken42/x")
    text = await (await (await aiohttp_client(make_app(ctx))).get("api/diagnose")).text()
    assert "Ingr3ssT0ken42" not in text and "Mozilla" not in text


async def test_probe_accepts_the_order_when_the_list_is_locked_by_it(tmp_path):
    from app.ahclient import ActiveOrder, AhOrderMode
    ah = FakeAh(tmp_path, order=ActiveOrder(5, {}))
    ctx = make_ctx(tmp_path, ah=ah)

    async def locked():
        raise AhOrderMode("order mode", 400)

    ah.shopping_list = locked
    # like AhClient: a successful read reports itself to ApiHealth
    real_periods, real_order = ah.bonus_periods, ah.active_order

    async def periods():
        out = await real_periods()
        ctx.health.ok("bonus")
        return out

    async def order():
        out = await real_order()
        ctx.health.ok("bestelling")
        return out

    ah.bonus_periods, ah.active_order = periods, order
    paused(ctx, "lijst")
    await main.probe_health(ctx)
    assert not ctx.health.writes_paused


async def test_last_ah_sync_survives_a_restart(tmp_path):
    ctx = make_ctx(tmp_path)
    ctx.health.ok("bonus")
    main._save_state(ctx)
    again = make_ctx(tmp_path)
    main.load_state(again)
    assert again.health.last_ok == ctx.health.last_ok


async def test_logout_waits_for_a_running_refresh(aiohttp_client, tmp_path):
    ctx = make_ctx(tmp_path)
    ctx.store.save(Tokens(ACCESS, REFRESH, 9e9))

    async def refresh_late():
        await asyncio.sleep(0.05)
        ctx.store.save(Tokens("new-a", "new-r", 9e9))  # what a refresh would write

    ctx.client._refresh_task = asyncio.create_task(refresh_late())
    client = await aiohttp_client(make_app(ctx))
    assert (await client.post("api/logout")).status == 200
    await asyncio.sleep(0.1)
    assert not (tmp_path / "tokens.json").exists()
