"""v0.8: notify targets as options, Vanavond, the order reminder, the login line."""
import json
from datetime import date, datetime, time
from pathlib import Path

import httpx
import pytest
import yaml

from app import main
from app.hass import BASE_URL, DEFAULT_OWNER, DEFAULT_TODAY, Hass, notify_target

ROOT = Path(__file__).resolve().parent.parent


# --- 1. notify targets are options ------------------------------------------------------


def test_config_has_the_notify_options_and_dinner_time():
    config = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    assert config["options"]["notify_owner"] == "mobile_app_s_26_ultra_anto"
    assert config["options"]["notify_today"] == ["mobile_app_s24_van_maartje"]
    assert config["options"]["dinner_time"] == "18:30"
    assert DEFAULT_OWNER == "mobile_app_s_26_ultra_anto"
    assert DEFAULT_TODAY == ["mobile_app_s24_van_maartje"]


def test_no_fixed_notify_name_in_hass():
    text = (ROOT / "app" / "hass.py").read_text(encoding="utf-8")
    assert "NOTIFY_SERVICE" not in text
    assert text.count("mobile_app_s_26_ultra_anto") == 1  # only the default


@pytest.mark.parametrize("raw, want", [
    ("mobile_app_x", "mobile_app_x"),
    (" notify.mobile_app_x ", "mobile_app_x"),
    ("notify/mobile_app_x", "mobile_app_x"),
    ("Mobile_App_X", "mobile_app_x"),
    ("../states/sensor.x", None),
    ("mobile app", None),
    ("", None),
    (None, None),
    (5, None),
])
def test_notify_target_is_a_bare_service_name(raw, want):
    assert notify_target(raw) == want


def test_load_notify_options(tmp_path):
    path = tmp_path / "options.json"
    path.write_text(json.dumps({"notify_owner": "notify.mobile_app_a",
                                "notify_today": ["mobile_app_b", "bad name", "mobile_app_c"],
                                "dinner_time": "19:00"}), encoding="utf-8")
    assert main.load_notify_options(path) == ("mobile_app_a", ["mobile_app_b", "mobile_app_c"],
                                              time(19, 0))
    assert main.load_notify_options(tmp_path / "nope.json") == (
        DEFAULT_OWNER, DEFAULT_TODAY, time(18, 30))
    path.write_text(json.dumps({"notify_owner": "../x", "notify_today": [],
                                "dinner_time": "25:99"}), encoding="utf-8")
    # a broken owner falls back to the default; an empty list means nobody gets Vanavond
    assert main.load_notify_options(path) == (DEFAULT_OWNER, [], time(18, 30))


async def test_owner_and_today_targets(respx_mock):
    owner = respx_mock.post(f"{BASE_URL}/services/notify/mobile_app_owner").respond(200, json=[])
    other = respx_mock.post(f"{BASE_URL}/services/notify/mobile_app_other").respond(200, json=[])
    async with httpx.AsyncClient() as http:
        h = Hass(http, owner="mobile_app_owner", today=["mobile_app_other"])
        await h.notify("T", "M")
        await h.notify_to("mobile_app_other", "Weekmenu", "Vanavond: x")
    assert owner.call_count == 1 and other.call_count == 1
    assert h.today == ["mobile_app_other"]
    with pytest.raises(ValueError):
        await h.notify_to("../states/x", "T", "M")


# --- 2. Vanavond ---------------------------------------------------------------------

from app.hass import start_time, vanavond  # noqa: E402
from app.ledger import WeekLog  # noqa: E402
from app.tokens import Tokens  # noqa: E402
from tests.fakes import W, FakeAh, FakeHass, dish, make_ctx, make_plan  # noqa: E402

THU = date(2026, 11, 5)
FRI = date(2026, 11, 6)
SUN = date(2026, 11, 8)


@pytest.mark.parametrize("cook, oven, wait, want", [
    (25, None, None, time(18, 0)),     # 18:05 rounds down to 18:00
    (30, None, None, time(18, 0)),
    (35, None, None, time(17, 45)),    # 17:55
    (20, 120, None, time(16, 0)),      # 16:10
    (30, None, 150, time(15, 30)),
    (0, None, None, time(18, 30)),
    (None, None, None, None),
])
def test_start_time_is_dinner_minus_all_times_rounded_down_to_a_quarter(cook, oven, wait,
                                                                        want):
    assert start_time(time(18, 30), cook, oven, wait) == want


def week_plan():
    """wo: 101 (and its leftovers on do), vr: 102 without a time, za removed."""
    a = dish(W, 101, [1], 2.0)
    a.cook, a.oven = 25, 20
    a.leftover_date = THU
    b = dish(FRI, 102, [2], 1.0)
    b.cook = None
    plan = make_plan([a, b])
    plan.removed_days = [date(2026, 11, 7)]
    return plan


def test_vanavond_lines():
    plan = week_plan()
    assert vanavond(plan, W) == "Vanavond: Recept 101, begin om 17:45"
    assert vanavond(plan, W, dinner=time(19, 0)) == "Vanavond: Recept 101, begin om 18:15"
    assert vanavond(plan, THU) == "Vanavond: restjes van Recept 101"
    assert vanavond(plan, FRI) == "Vanavond: Recept 102"
    assert vanavond(plan, date(2026, 11, 7)) is None  # geen gerecht
    assert vanavond(plan, SUN) is None  # geen kookmoment
    assert vanavond(plan, W, away={"woensdag"}) is None  # niet thuis
    assert vanavond(None, W) is None


def tonight_ctx(tmp_path, now, today=("mobile_app_b",), plan=None):
    hass = FakeHass(tmp_path / "totaal.json", today=today)
    ctx = make_ctx(tmp_path, hass=hass, now=now)
    plan = plan or week_plan()
    ctx.ledger.save(WeekLog(W, plan=plan.to_dict(), target="list", round_done=True))
    return ctx


def tonights(ctx):
    return [(path, p["message"]) for path, p in ctx.hass.posts
            if path.startswith("services/notify/")]


async def test_vanavond_push_once_at_1600_to_notify_today_only(tmp_path):
    ctx = tonight_ctx(tmp_path, datetime(2026, 11, 4, 15, 0),
                      today=("mobile_app_b", "mobile_app_c"))
    await main.tick(ctx, datetime(2026, 11, 4, 15, 59))
    assert tonights(ctx) == []
    for minute in range(0, 120, 7):
        await main.tick(ctx, datetime(2026, 11, 4, 16 + minute // 60, minute % 60))
    msg = "Vanavond: Recept 101, begin om 17:45"
    assert tonights(ctx) == [("services/notify/mobile_app_b", msg),
                             ("services/notify/mobile_app_c", msg)]
    state = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert state["tonight_sent"] == "2026-11-04" and state["schema"] == 1

    # a restart the same afternoon: the mark comes back, no second push
    again = make_ctx(tmp_path, hass=ctx.hass, now=datetime(2026, 11, 4, 17, 0))
    main.load_state(again)
    await main.tick(again, datetime(2026, 11, 4, 17, 0))
    assert len(tonights(ctx)) == 2

    # the next day: leftovers
    await main.tick(again, datetime(2026, 11, 5, 16, 0))
    assert tonights(ctx)[2:] == [
        ("services/notify/mobile_app_b", "Vanavond: restjes van Recept 101"),
        ("services/notify/mobile_app_c", "Vanavond: restjes van Recept 101")]


async def test_no_vanavond_when_away_no_dish_or_no_menu(tmp_path):
    ctx = tonight_ctx(tmp_path, datetime(2026, 11, 4, 16, 0))
    ctx.hass.states["input_number.weekmenu_personen_woensdag"] = "0"
    await main.tick(ctx, datetime(2026, 11, 4, 16, 0))  # niet thuis
    await main.tick(ctx, datetime(2026, 11, 7, 16, 0))  # geen gerecht
    await main.tick(ctx, datetime(2026, 11, 8, 16, 30))  # geen kookmoment
    await main.tick(ctx, datetime(2026, 11, 12, 16, 0))  # no menu that week
    assert tonights(ctx) == []
    assert ctx.tonight_sent == date(2026, 11, 12)  # looked once a day, not every minute


async def test_vanavond_needs_targets_and_the_afternoon(tmp_path):
    ctx = tonight_ctx(tmp_path, datetime(2026, 11, 4, 16, 0), today=())
    await main.tick(ctx, datetime(2026, 11, 4, 16, 0))
    assert tonights(ctx) == []
    (tmp_path / "late").mkdir()
    ctx = tonight_ctx(tmp_path / "late", datetime(2026, 11, 4, 21, 0))
    await main.tick(ctx, datetime(2026, 11, 4, 21, 0))  # a start late in the day
    assert tonights(ctx) == []


async def test_vanavond_not_sent_when_the_mark_cannot_be_saved(tmp_path):
    ctx = tonight_ctx(tmp_path, datetime(2026, 11, 4, 16, 0))
    (tmp_path / "state.json").write_text('{"schema": 99}', encoding="utf-8")
    await main.tick(ctx, datetime(2026, 11, 4, 16, 0))
    assert not any("Vanavond" in m for _, m in tonights(ctx))


async def test_vanavond_on_the_sensor_and_it_moves_on_each_day(tmp_path):
    ctx = tonight_ctx(tmp_path, datetime(2026, 11, 4, 9, 0), today=())
    ctx.store.save(Tokens("a", "r", 9e9))
    await main.tick(ctx, datetime(2026, 11, 4, 9, 0))
    await main.tick(ctx, datetime(2026, 11, 4, 9, 1))  # same day: not again
    ctx.clock = lambda: datetime(2026, 11, 5, 0, 1)
    await main.tick(ctx, datetime(2026, 11, 5, 0, 1))
    sensors = [p["attributes"] for path, p in ctx.hass.posts
               if path == "states/sensor.weekmenu"]
    assert [a["vanavond"] for a in sensors] == ["Vanavond: Recept 101, begin om 17:45",
                                               "Vanavond: restjes van Recept 101"]
    assert sensors[0]["login_ok"] is True and sensors[0]["login_refreshed"]


# --- 3. the order reminder ----------------------------------------------------------------

from app.ahclient import ActiveOrder  # noqa: E402

NOV11 = date(2026, 11, 11)


def reminder_ctx(tmp_path, order=None, plan=True):
    ah = FakeAh(tmp_path, order=order)
    ctx = make_ctx(tmp_path, ah=ah, now=datetime(2026, 11, 8, 17, 0))
    ctx.store.save(Tokens("a", "r", 9e9))
    if plan:
        ctx.ledger.save(WeekLog(NOV11, plan=make_plan([dish(NOV11, 301, [6])]).to_dict(),
                                target="list", round_done=True))
    return ctx


def reminders(ctx):
    return [p for p in ctx.hass.notifications() if p["message"] == main.REMINDER]


async def test_reminder_on_sunday_1700_once(tmp_path):
    ctx = reminder_ctx(tmp_path)
    await main.tick(ctx, datetime(2026, 11, 8, 16, 59))
    assert reminders(ctx) == []
    for minute in range(0, 7 * 60, 5):
        await main.tick(ctx, datetime(2026, 11, 8, 17 + minute // 60, minute % 60))
    assert len(reminders(ctx)) == 1
    state = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert state["reminder_sent"] == "2026-11-08"
    again = make_ctx(tmp_path, ah=ctx.client, hass=ctx.hass, now=datetime(2026, 11, 8, 20, 0))
    main.load_state(again)
    await main.tick(again, datetime(2026, 11, 8, 20, 0))
    assert len(reminders(ctx)) == 1
    assert ctx.client.writes == []  # only reads


async def test_no_reminder_on_other_days_or_without_a_menu(tmp_path):
    ctx = reminder_ctx(tmp_path)
    await main.tick(ctx, datetime(2026, 11, 7, 17, 0))  # Saturday
    assert reminders(ctx) == []
    (tmp_path / "b").mkdir()
    ctx = reminder_ctx(tmp_path / "b", plan=False)
    await main.tick(ctx, datetime(2026, 11, 8, 17, 0))
    assert reminders(ctx) == []


@pytest.mark.parametrize("delivery, want", [
    (date(2026, 11, 11), 0),   # an order in the menu week: chosen
    (date(2026, 11, 17), 0),   # the last day of the menu week
    (date(2026, 11, 4), 1),    # an order for this week only: still nothing chosen
    (None, 0),                 # an order whose day is unknown: no guess, no push
])
async def test_reminder_looks_at_the_delivery_day(tmp_path, delivery, want):
    ctx = reminder_ctx(tmp_path, order=ActiveOrder(7, {}, delivery))
    await main.tick(ctx, datetime(2026, 11, 8, 17, 0))
    assert len(reminders(ctx)) == want


async def test_reminder_retries_a_failed_read_an_hour_later(tmp_path):
    ctx = reminder_ctx(tmp_path)
    ctx.client.raise_on["active_order"] = httpx.ConnectError("weg")
    await main.tick(ctx, datetime(2026, 11, 8, 17, 0))
    del ctx.client.raise_on["active_order"]
    await main.tick(ctx, datetime(2026, 11, 8, 17, 30))
    assert reminders(ctx) == []
    await main.tick(ctx, datetime(2026, 11, 8, 18, 0))
    assert len(reminders(ctx)) == 1


async def test_no_reminder_without_a_login(tmp_path):
    ctx = reminder_ctx(tmp_path)
    ctx.login_expired = True
    await main.tick(ctx, datetime(2026, 11, 8, 17, 0))
    assert reminders(ctx) == []


# --- 4. the login line ----------------------------------------------------------------


def test_login_view_ok_stale_and_expired(tmp_path):
    from datetime import timedelta
    ctx = make_ctx(tmp_path, now=datetime(2026, 11, 11, 12, 0))
    assert main.login_view(ctx)["login_ok"] is False  # never logged in
    assert main.login_view(ctx)["login_refreshed"] is None
    ctx.store.save(Tokens("a", "r", 9e9))
    stamp = main.local_from_epoch(ctx.store.saved_at())
    view = main.login_view(ctx, stamp)
    assert view == {"login_ok": True, "login_refreshed": stamp.isoformat(timespec="minutes"),
                    "login_expired": False, "login_stale": False}
    late = main.login_view(ctx, stamp + timedelta(days=7, minutes=1))
    assert late["login_ok"] is False and late["login_stale"] is True
    ctx.login_expired = True
    view = main.login_view(ctx, stamp)
    assert view["login_ok"] is False and view["login_expired"] is True


# --- api/week and the sensor from the web API ------------------------------------------


async def test_api_week_has_vanavond_and_login(tmp_path, aiohttp_client):
    from app.web import make_app
    ctx = tonight_ctx(tmp_path, datetime(2026, 11, 5, 12, 0), today=())
    ctx.store.save(Tokens("a", "r", 9e9))
    client = await aiohttp_client(make_app(ctx))
    body = await (await client.get("api/week")).json()
    assert body["vanavond"] == "Vanavond: restjes van Recept 101"
    assert body["login"]["login_ok"] is True and body["login"]["login_refreshed"]
    ctx.login_expired = True
    body = await (await client.get("api/week")).json()
    assert body["login"]["login_ok"] is False and body["login"]["login_expired"] is True


# --- 8. a refused request is logged: remote and path only -----------------------------


async def test_refused_request_is_logged_with_remote_and_path_only(tmp_path, aiohttp_client,
                                                                   caplog):
    import logging
    from app.web import make_app
    ctx = make_ctx(tmp_path)
    client = await aiohttp_client(make_app(ctx, allowed_remotes={"172.30.32.2"}))
    with caplog.at_level(logging.WARNING, logger="weekmenu.web"):
        r = await client.get("/api/health?token=geheim-123",
                             headers={"Referer": "http://x/api/hassio_ingress/tok-999/"})
    assert r.status == 403
    lines = [rec.getMessage() for rec in caplog.records if rec.name == "weekmenu.web"]
    assert lines == ["Verzoek geweigerd (niet via Home Assistant): 127.0.0.1 /api/health"]
    text = caplog.text
    assert "geheim-123" not in text and "tok-999" not in text and "Referer" not in text


async def test_refused_path_is_cut_and_has_no_line_breaks(tmp_path, aiohttp_client, caplog):
    import logging
    from app.web import make_app
    ctx = make_ctx(tmp_path)
    client = await aiohttp_client(make_app(ctx, allowed_remotes={"172.30.32.2"}))
    with caplog.at_level(logging.WARNING, logger="weekmenu.web"):
        await client.get("/a%0Ab" + "x" * 300)
    (line,) = [rec.getMessage() for rec in caplog.records if rec.name == "weekmenu.web"]
    assert "\n" not in line and len(line) < 200
