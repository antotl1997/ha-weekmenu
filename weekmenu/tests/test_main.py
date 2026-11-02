import json
from datetime import date, datetime

import pytest

from app import main
from app.ahclient import AhApiError, AhLoginExpired, AhNotJson, BonusPeriod
from app.ledger import Ledger, WeekLog
from app.planner import Plan
from app.hass import helper_persons
from app.main import PERIOD_WARNING, handle_command, run_round, tick, update_total
from tests.fakes import CURRENT, NEXT, SUNDAY, W, FakeAh, FakeHass, dish, make_ctx, make_plan

WO, DO, VR = W, date(2026, 11, 5), date(2026, 11, 6)


def plan_a():
    return make_plan([dish(WO, 101, [1, 2], 2.0), dish(DO, 102, [2, 3], 3.0)])


def plan_b():
    return make_plan([dish(WO, 201, [1, 4], 1.0), dish(VR, 202, [5], 2.0)])


@pytest.fixture
def planned(monkeypatch):
    """build_plan returns the queued plans in turn, and records its Settings."""
    calls = []
    queue = [plan_a()]

    async def fake_build(client, bonus, s):
        calls.append(s)
        return queue.pop(0) if len(queue) > 1 else queue[0]

    monkeypatch.setattr(main, "build_plan", fake_build)
    return calls, queue


# --- the brief's tests -------------------------------------------------------------


async def test_after_18_falls_back_to_current_period_with_warning(tmp_path, planned):
    ah = FakeAh(tmp_path, periods=[CURRENT])
    ctx = make_ctx(tmp_path, ah=ah)
    out = await run_round(ctx, datetime(2026, 11, 1, 18, 0), "schema")
    assert out.status == "done" and out.warning == PERIOD_WARNING
    assert ah.offers_for == [CURRENT]
    (note,) = ctx.hass.notifications()
    assert "prijzen gelden woensdag waarschijnlijk niet meer" in note["message"]


async def test_nu_maken_reuses_existing_plan(tmp_path, monkeypatch):
    async def no_build(*a, **k):
        raise AssertionError("build_plan must not run again")

    monkeypatch.setattr(main, "build_plan", no_build)
    ctx = make_ctx(tmp_path)
    ctx.ledger.save(WeekLog(W, plan=plan_a().to_dict(), added={1: 1}))
    out = await run_round(ctx, SUNDAY, "nu_maken")
    assert out.status == "done"
    # the menu does not read the folder again; only the staples step after it does
    assert "bonus_offers" not in ctx.client.calls
    assert ctx.client.calls == ["bonus_periods", "previously_bought_bonus", "purchases"]
    assert ctx.client.held() == {2: 2, 3: 1}  # only what the log does not have yet


async def test_nieuw_menu_replaces_week_savings_not_adds(tmp_path, planned):
    calls, queue = planned
    queue[:] = [plan_a(), plan_b()]
    ctx = make_ctx(tmp_path)
    await run_round(ctx, SUNDAY, "nu_maken")
    assert ctx.ledger.load(W).savings == 5.0
    out = await run_round(ctx, SUNDAY, "nieuw_menu")
    assert out.status == "done"
    log = ctx.ledger.load(W)
    assert log.savings == 3.0  # replaced, not 8.0
    assert {101, 102} <= calls[1].recent_recipe_ids  # a different menu, not the same one
    assert ctx.client.held() == {1: 1, 2: 2, 3: 1, 4: 1, 5: 1}  # product 1 not again
    note = ctx.hass.notifications()[-1]["message"]
    assert "Restjes van het oude menu" in note and "Product 2 (2x)" in note


async def test_login_expired_notifies_and_writes_nothing(tmp_path, planned):
    ah = FakeAh(tmp_path)
    ah.raise_on["bonus_offers"] = AhLoginExpired("login expired", 401)
    ctx = make_ctx(tmp_path, ah=ah, ingress_url="/hassio/ingress/abc_weekmenu")
    out = await run_round(ctx, SUNDAY, "schema")
    assert out.status == "login_expired"
    assert ah.writes == []
    (note,) = ctx.hass.notifications()
    assert note["message"] == "AH-login verlopen, log opnieuw in"
    assert note["data"]["clickAction"] == "/hassio/ingress/abc_weekmenu"
    assert not any(path == "states/sensor.weekmenu" for path, _ in ctx.hass.posts)
    assert ctx.login_expired and ctx.error


async def test_disabled_skips_schema_but_not_buttons(tmp_path, planned):
    calls, _ = planned
    ctx = make_ctx(tmp_path, hass=FakeHass(tmp_path / "totaal.json", enabled=False))
    out = await run_round(ctx, SUNDAY, "schema")
    assert out.status == "skipped" and ctx.client.calls == [] and calls == []
    out = await run_round(ctx, SUNDAY, "nu_maken")
    assert out.status == "done" and ctx.client.held() == {1: 1, 2: 2, 3: 1}


async def test_restart_mid_write_then_schema_adds_no_duplicates(tmp_path, planned):
    calls, _ = planned
    # the 2nd write reaches AH, then the connection drops (like a crash after the call)
    ah = FakeAh(tmp_path, fail_on_call=1, fail_after=True)
    ctx = make_ctx(tmp_path, ah=ah)
    out = await run_round(ctx, SUNDAY, "schema")
    assert out.status == "error"
    assert "boom" in ctx.hass.notifications()[0]["message"]
    assert ("Tik op Nu maken in de Weekmenu-app om de rest toe te voegen."
            in ctx.hass.notifications()[0]["message"])
    assert len(ah.writes) == 2  # stopped, nothing retried
    assert ctx.ledger.load(W).round_done is False

    # "restart": a fresh context on the same /data, same AH state
    ah.fail_on_call = None
    ctx2 = make_ctx(tmp_path, ah=ah)
    ctx2.ledger = Ledger(tmp_path / "weeks")
    # the round did not finish: the next automatic slot completes it, once
    out = await run_round(ctx2, datetime(2026, 11, 1, 9, 0), "schema")
    assert out.status == "done"
    assert len(calls) == 1  # the plan of the first round is reused
    assert ah.held() == plan_a().basket == {1: 1, 2: 2, 3: 1}
    assert len(ah.writes) == 3  # the write AH had applied was settled, not repeated
    assert ctx2.ledger.load(W).round_done is True
    assert len(ctx2.hass.notifications()) == 1  # the "klaar" message, once
    out = await run_round(ctx2, datetime(2026, 11, 1, 10, 0), "schema")
    assert out.status == "skipped"
    # and once more: nothing at all
    n = len(ah.writes)
    await run_round(ctx2, datetime(2026, 11, 1, 9, 10), "nu_maken")
    assert len(ah.writes) == n


async def test_week_counted_once_in_total(tmp_path, planned):
    ctx = make_ctx(tmp_path)
    (tmp_path / "totaal.json").write_text(json.dumps({"total": 10.0}), encoding="utf-8")
    ctx.ledger.save(WeekLog(date(2026, 10, 28), plan=plan_b().to_dict(), savings=4.0))
    await run_round(ctx, SUNDAY, "schema")
    await run_round(ctx, SUNDAY, "nu_maken")
    await update_total(ctx, SUNDAY.date())
    assert ctx.hass.totals() == [14.0, 14.0, 14.0]
    assert json.loads((tmp_path / "totaal.json").read_text())["total"] == 14.0
    assert ctx.ledger.load(date(2026, 10, 28)).counted
    assert not ctx.ledger.load(W).counted  # starts on Wednesday, counted then


# --- extra coverage ----------------------------------------------------------------


async def test_failing_home_assistant_never_breaks_the_round(tmp_path, planned):
    ctx = make_ctx(tmp_path, hass=FakeHass(tmp_path / "totaal.json", fail_posts=True))
    out = await run_round(ctx, SUNDAY, "nu_maken")
    assert out.status == "done" and ctx.client.held() == {1: 1, 2: 2, 3: 1}


async def test_ah_html_response_notifies_and_writes_nothing(tmp_path, planned):
    ah = FakeAh(tmp_path)
    ah.raise_on["bonus_offers"] = AhNotJson("GET /x: response is not JSON", 403)
    ctx = make_ctx(tmp_path, ah=ah)
    out = await run_round(ctx, SUNDAY, "nu_maken")
    assert out.status == "error" and ah.writes == []
    assert "not JSON" in ctx.hass.notifications()[0]["message"]
    assert ctx.ledger.load(W).plan is None


async def test_empty_plan_is_not_written(tmp_path, monkeypatch):
    async def empty(*a, **k):
        return make_plan([], short_by=5)

    monkeypatch.setattr(main, "build_plan", empty)
    ctx = make_ctx(tmp_path)
    ctx.ledger.save(WeekLog(W, plan=plan_a().to_dict(), added=dict(plan_a().basket)))
    out = await run_round(ctx, SUNDAY, "nieuw_menu")
    assert out.status == "error"
    assert ctx.ledger.load(W).plan == plan_a().to_dict()  # the old menu stays


async def test_settings_follow_helpers(tmp_path, planned):
    calls, _ = planned
    hass = FakeHass(tmp_path / "totaal.json", aantal=3)
    hass.states["input_number.weekmenu_personen_donderdag"] = "0"
    hass.states["input_number.weekmenu_personen_vrijdag"] = "4"
    ctx = make_ctx(tmp_path, hass=hass)
    await run_round(ctx, SUNDAY, "nu_maken")
    s = calls[0]
    assert s.evenings == [WO, VR, date(2026, 11, 7)]
    assert s.persons[VR] == 4 and s.persons[WO] == 2
    assert s.pantry == ctx.pantry


async def test_stdin_commands(tmp_path, planned):
    ctx = make_ctx(tmp_path)
    out = await handle_command(ctx, '{"command": "nu_maken"}\n')
    assert out.status == "done"
    assert await handle_command(ctx, '{"command": "rm -rf"}') is None
    assert await handle_command(ctx, "niet eens json") is None
    assert await handle_command(ctx, "") is None


# --- fix round 1 (Ruling 22) -------------------------------------------------------


async def test_total_not_counted_twice_after_crash_between_file_and_flags(
    tmp_path, planned, monkeypatch
):
    ctx = make_ctx(tmp_path)
    (tmp_path / "totaal.json").write_text('{"total": 10.0, "counted": []}', encoding="utf-8")
    ctx.ledger.save(WeekLog(date(2026, 10, 28), plan=plan_b().to_dict(), savings=4.0))
    real_save = ctx.ledger.save

    def crash(log):
        raise SystemExit("power cut")  # not an Exception: like the process dying

    monkeypatch.setattr(ctx.ledger, "save", crash)
    with pytest.raises(SystemExit):
        await update_total(ctx, SUNDAY.date())
    data = json.loads((tmp_path / "totaal.json").read_text())
    assert data == {"total": 14.0, "counted": {"2026-10-28": 4.0}}
    assert not Ledger(tmp_path / "weeks").load(date(2026, 10, 28)).counted  # flag lost

    monkeypatch.setattr(ctx.ledger, "save", real_save)
    await update_total(ctx, SUNDAY.date())
    assert json.loads((tmp_path / "totaal.json").read_text())["total"] == 14.0
    assert ctx.hass.totals()[-1] == 14.0


@pytest.mark.parametrize("old", ["12.5", '{"total": 12.5}'])
async def test_old_total_file_is_migrated_from_ledger_flags(tmp_path, planned, old):
    ctx = make_ctx(tmp_path)
    (tmp_path / "totaal.json").write_text(old, encoding="utf-8")
    ctx.ledger.save(WeekLog(date(2026, 10, 21), plan=plan_b().to_dict(), savings=3.0,
                            counted=True))
    ctx.ledger.save(WeekLog(date(2026, 10, 28), plan=plan_b().to_dict(), savings=4.0))
    await update_total(ctx, SUNDAY.date())
    data = json.loads((tmp_path / "totaal.json").read_text())
    assert data == {"total": 16.5, "counted": {"2026-10-21": 3.0, "2026-10-28": 4.0}}


async def test_reused_plan_adding_nothing_sends_no_second_message(tmp_path, planned):
    ctx = make_ctx(tmp_path)
    await run_round(ctx, datetime(2026, 10, 31, 12, 0), "nu_maken")  # Saturday button
    assert len(ctx.hass.notifications()) == 1
    out = await run_round(ctx, SUNDAY, "nu_maken")
    assert out.status == "done" and out.result.added == {}
    assert len(ctx.hass.notifications()) == 1  # no second "klaar"
    assert sum(p == "states/sensor.weekmenu" for p, _ in ctx.hass.posts) == 2  # still published
    # the automatic round sees the plan in the log and does nothing at all
    out = await run_round(ctx, datetime(2026, 11, 1, 10, 0), "schema")
    assert out.status == "skipped"
    assert len(ctx.hass.notifications()) == 1
    assert sum(p == "states/sensor.weekmenu" for p, _ in ctx.hass.posts) == 2


async def test_period_warning_is_kept_in_the_log_and_cleared_by_a_proper_round(
        tmp_path, planned):
    ah = FakeAh(tmp_path, periods=[CURRENT])
    ctx = make_ctx(tmp_path, ah=ah)
    await run_round(ctx, datetime(2026, 11, 1, 18, 0), "schema")
    assert ctx.ledger.load(W).period_warning == PERIOD_WARNING
    # nu_maken reuses the plan: the warning about its prices stays
    await run_round(ctx, datetime(2026, 11, 1, 19, 0), "nu_maken")
    assert ctx.ledger.load(W).period_warning == PERIOD_WARNING
    # a new menu from the right folder clears it
    ah.periods = [CURRENT, NEXT]
    await run_round(ctx, datetime(2026, 11, 1, 19, 0), "nieuw_menu")
    assert ctx.ledger.load(W).period_warning is None


def test_weeklog_without_period_warning_loads():
    d = WeekLog(W).to_dict()
    del d["period_warning"]
    assert WeekLog.from_dict(d).period_warning is None
    log = WeekLog(W, period_warning="x")
    assert WeekLog.from_dict(log.to_dict()).period_warning == "x"


async def test_old_counted_list_is_migrated_with_ledger_values(tmp_path, planned):
    ctx = make_ctx(tmp_path)
    (tmp_path / "totaal.json").write_text('{"total": 3.0, "counted": ["2026-10-21"]}',
                                          encoding="utf-8")
    ctx.ledger.save(WeekLog(date(2026, 10, 21), plan=plan_b().to_dict(), savings=3.0,
                            extras_savings=0.5, counted=True))
    await update_total(ctx, SUNDAY.date())
    data = json.loads((tmp_path / "totaal.json").read_text())
    # the list had no amounts: the ledger value at migration time counts as counted
    assert data == {"total": 3.0, "counted": {"2026-10-21": 3.5}}


async def test_staple_added_after_week_was_counted_reaches_total_once(tmp_path, planned):
    ctx = make_ctx(tmp_path)
    old = date(2026, 10, 28)
    ctx.ledger.save(WeekLog(old, plan=plan_b().to_dict(), savings=4.0))
    await update_total(ctx, SUNDAY.date())
    assert ctx.hass.totals()[-1] == 4.0
    log = ctx.ledger.load(old)
    log.extras_added = {50: 2}
    log.extras_savings = 1.25  # tapped in the app after the week began
    ctx.ledger.save(log)
    await update_total(ctx, SUNDAY.date())
    assert ctx.hass.totals()[-1] == 5.25
    await update_total(ctx, SUNDAY.date())
    await update_total(ctx, SUNDAY.date())
    assert ctx.hass.totals()[-1] == 5.25
    data = json.loads((tmp_path / "totaal.json").read_text())
    assert data == {"total": 5.25, "counted": {"2026-10-28": 5.25}}


async def test_total_never_goes_down(tmp_path, planned):
    ctx = make_ctx(tmp_path)
    old = date(2026, 10, 28)
    ctx.ledger.save(WeekLog(old, plan=plan_b().to_dict(), savings=4.0))
    await update_total(ctx, SUNDAY.date())
    log = ctx.ledger.load(old)
    log.savings = 2.0  # e.g. a swap to a cheaper dish after the week began
    ctx.ledger.save(log)
    await update_total(ctx, SUNDAY.date())
    assert ctx.hass.totals()[-1] == 4.0
    data = json.loads((tmp_path / "totaal.json").read_text())
    assert data["counted"] == {"2026-10-28": 4.0}


# --- final review fixes ----------------------------------------------------------------


async def test_refused_refresh_in_a_round_asks_for_login(tmp_path, planned, respx_mock,
                                                         aiohttp_client):
    import time as _time

    import httpx

    from app.ahclient import AhClient
    from app.tokens import Tokens
    from app.web import make_app

    respx_mock.post("https://api.ah.nl/mobile-auth/v1/auth/token/refresh").respond(
        400, json={"error": "invalid_grant"})
    ah = FakeAh(tmp_path)
    ah.store.save(Tokens("a", "r", _time.time() + 10))  # about to expire
    async with httpx.AsyncClient() as http:
        real = AhClient(ah.store, http)
        ah.bonus_periods = real.bonus_periods  # the first AH call refreshes the token
        ctx = make_ctx(tmp_path, ah=ah)
        out = await run_round(ctx, SUNDAY, "schema")
    assert out.status == "login_expired" and ah.writes == []
    (note,) = ctx.hass.notifications()
    assert note["message"] == "AH-login verlopen, log opnieuw in"
    assert ctx.login_expired
    client = await aiohttp_client(make_app(ctx))
    status = await (await client.get("/api/status")).json()
    assert status["logged_in"] is False  # app.js shows the login screen


# --- schedule: as soon as the folder is out (2 November 2026) ------------------------------

# Real AH data: periods run Monday to Sunday; the next one shows from the Friday before.
CUR = BonusPeriod(date(2026, 10, 26), date(2026, 11, 1), date(2026, 10, 30), ["Vlees"])
NXT = BonusPeriod(date(2026, 11, 2), date(2026, 11, 8), date(2026, 11, 6), ["Vlees"])
NXT2 = BonusPeriod(date(2026, 11, 9), date(2026, 11, 15), date(2026, 11, 13), ["Vlees"])
FRI, SAT, SUN, MON, WED = (date(2026, 10, 30), date(2026, 10, 31), date(2026, 11, 1),
                           date(2026, 11, 2), date(2026, 11, 4))


def at(day, hh, mm=0):
    return datetime(day.year, day.month, day.day, hh, mm)


async def whole_day(ctx, day):
    """The schedule loop over one day, a look every 30 minutes."""
    for minute in range(0, 24 * 60, 30):
        await tick(ctx, at(day, minute // 60, minute % 60))


def first_lines(ctx):
    return [n["message"].splitlines()[0] for n in ctx.hass.notifications()]


async def test_friday_0800_without_folder_does_nothing(tmp_path, planned):
    calls, _ = planned
    ah = FakeAh(tmp_path, periods=[CUR])
    ctx = make_ctx(tmp_path, ah=ah)
    await tick(ctx, at(FRI, 7, 30))
    assert ah.calls == []  # before schedule_time: not even a look
    await tick(ctx, at(FRI, 8, 0))
    assert ah.calls == ["bonus_periods"]  # one look, no bonus_offers
    await tick(ctx, at(FRI, 8, 30))
    assert ah.calls == ["bonus_periods"]  # at most once per hour
    assert calls == [] and ah.writes == [] and ctx.hass.notifications() == []
    assert ctx.ledger.load(W).plan is None
    assert ctx.waiting == main.Waiting(W, date(2026, 10, 30))
    assert ctx.error is None and ctx.last_round is None


async def test_friday_0900_folder_out_makes_menu_and_one_message(tmp_path, planned):
    calls, _ = planned
    ah = FakeAh(tmp_path, periods=[CUR])
    ctx = make_ctx(tmp_path, ah=ah)
    await tick(ctx, at(FRI, 8, 0))
    ah.periods = [CUR, NXT]  # the folder appears
    await tick(ctx, at(FRI, 9, 0))
    assert len(calls) == 1 and ah.offers_for == [NXT]
    assert ah.held() == {1: 1, 2: 2, 3: 1}
    (note,) = ctx.hass.notifications()
    assert note["message"].splitlines()[0] == "Voor de bestelling van woensdag 4 november"
    assert PERIOD_WARNING not in note["message"]
    assert ctx.waiting is None


async def test_no_second_round_on_saturday_or_sunday(tmp_path, planned):
    calls, _ = planned
    ah = FakeAh(tmp_path, periods=[CUR, NXT])
    ctx = make_ctx(tmp_path, ah=ah)
    await whole_day(ctx, FRI)
    assert len(calls) == 1 and len(ctx.hass.notifications()) == 1
    n_calls, n_writes, n_posts = len(ah.calls), len(ah.writes), len(ctx.hass.posts)
    await whole_day(ctx, SAT)
    await whole_day(ctx, SUN)
    assert len(calls) == 1 and len(ctx.hass.notifications()) == 1
    assert len(ah.calls) == n_calls and len(ah.writes) == n_writes  # not even a read
    # only the daily total is published again, never the menu
    assert all(p == "states/sensor.weekmenu_besparing_totaal"
               for p, _ in ctx.hass.posts[n_posts:])


async def test_sunday_1800_without_folder_falls_back_with_warning(tmp_path, planned):
    calls, _ = planned
    ah = FakeAh(tmp_path, periods=[CUR])
    ctx = make_ctx(tmp_path, ah=ah)
    await whole_day(ctx, FRI)
    await whole_day(ctx, SAT)
    assert calls == [] and ctx.hass.notifications() == []  # Friday/Saturday 18:00: no fallback
    for hh in range(0, 18):
        await tick(ctx, at(SUN, hh, 0))
        await tick(ctx, at(SUN, hh, 30))
    assert calls == [] and "bonus_offers" not in ah.calls
    assert ah.calls.count("bonus_periods") == 11 + 11 + 10  # hourly 08:00-18:00, no more
    await tick(ctx, at(SUN, 18, 0))
    assert len(calls) == 1 and ah.offers_for == [CUR]
    (note,) = ctx.hass.notifications()
    lines = note["message"].splitlines()
    assert lines[0] == "Voor de bestelling van woensdag 4 november" and lines[1] == PERIOD_WARNING
    assert ctx.ledger.load(W).period_warning == PERIOD_WARNING
    await tick(ctx, at(SUN, 18, 30))
    await tick(ctx, at(SUN, 23, 0))
    assert len(calls) == 1 and len(ctx.hass.notifications()) == 1


async def test_monday_and_wednesday_after_failed_fallback_do_nothing(tmp_path, planned):
    calls, _ = planned
    ah = FakeAh(tmp_path, periods=[CUR])
    ah.raise_on["bonus_offers"] = AhApiError("boom", 500)
    ctx = make_ctx(tmp_path, ah=ah)
    for hh in range(18, 23):  # the fallback fails: tried every hour, quietly
        await tick(ctx, at(SUN, hh, 0))
    assert ctx.hass.notifications() == []
    await tick(ctx, at(SUN, 23, 0))  # the last try fails too: reported once
    await tick(ctx, at(SUN, 23, 30))
    assert ctx.ledger.load(W).plan is None
    (note,) = ctx.hass.notifications()
    assert note["message"] == main.FINAL_FAILURE
    assert ah.calls.count("bonus_offers") == 6
    del ah.raise_on["bonus_offers"]
    ah.periods = [CUR, NXT]  # Monday: the folder of the week of 4 November is there now
    ah.calls.clear()
    await whole_day(ctx, MON)
    await whole_day(ctx, date(2026, 11, 3))
    await whole_day(ctx, WED)
    # the target is now the week of 11 November, whose folder is not out: only looks
    assert calls == [] and ah.writes == [] and len(ctx.hass.notifications()) == 1
    assert set(ah.calls) == {"bonus_periods"} and len(ah.calls) == 3 * 11
    assert ctx.waiting == main.Waiting(date(2026, 11, 11), date(2026, 11, 6))


async def test_monday_after_fallback_menu_makes_no_new_menu(tmp_path, planned):
    calls, _ = planned
    ah = FakeAh(tmp_path, periods=[CUR])
    ctx = make_ctx(tmp_path, ah=ah)
    await tick(ctx, at(SUN, 18, 0))
    assert len(calls) == 1
    await whole_day(ctx, MON)
    await whole_day(ctx, WED)
    assert len(calls) == 1 and len(ctx.hass.notifications()) == 1


async def test_nu_maken_before_folder_waits_and_writes_nothing(tmp_path, planned):
    calls, _ = planned
    ah = FakeAh(tmp_path, periods=[CUR])
    ctx = make_ctx(tmp_path, ah=ah)
    for mode in ("nu_maken", "nieuw_menu"):
        out = await run_round(ctx, at(date(2026, 10, 29), 10, 0), mode)
        assert out.status == "waiting_for_folder" and out.expected == date(2026, 10, 30)
    assert calls == [] and ah.writes == [] and "bonus_offers" not in ah.calls
    assert ctx.ledger.load(W).plan is None and ctx.hass.notifications() == []
    assert ctx.waiting == main.Waiting(W, date(2026, 10, 30)) and ctx.error is None
    # the folder is out: the same button makes the menu, and the waiting is over
    ah.periods = [CUR, NXT]
    out = await run_round(ctx, at(FRI, 10, 0), "nu_maken")
    assert out.status == "done" and ctx.waiting is None and ah.offers_for == [NXT]


async def test_nu_maken_after_sunday_1800_may_use_the_current_folder(tmp_path, planned):
    ah = FakeAh(tmp_path, periods=[CUR])
    ctx = make_ctx(tmp_path, ah=ah)
    out = await run_round(ctx, at(SUN, 17, 59), "nu_maken")
    assert out.status == "waiting_for_folder"
    out = await run_round(ctx, at(SUN, 19, 0), "nu_maken")
    assert out.status == "done" and out.warning == PERIOD_WARNING


async def test_restart_while_waiting_runs_no_double_round(tmp_path, planned):
    calls, _ = planned
    ah = FakeAh(tmp_path, periods=[CUR])
    ctx = make_ctx(tmp_path, ah=ah)
    await tick(ctx, at(FRI, 8, 0))
    assert ah.calls == ["bonus_periods"]
    ctx2 = make_ctx(tmp_path, ah=ah)  # restart at 08:20
    main.load_state(ctx2)
    assert ctx2.waiting == main.Waiting(W, date(2026, 10, 30))
    await tick(ctx2, at(FRI, 8, 20))
    assert ah.calls == ["bonus_periods"]  # this hour was already looked at
    ah.periods = [CUR, NXT]
    await tick(ctx2, at(FRI, 9, 0))
    assert len(calls) == 1 and len(ctx2.hass.notifications()) == 1
    ctx3 = make_ctx(tmp_path, ah=ah)  # and a restart after the menu: nothing more
    main.load_state(ctx3)
    n = len(ah.calls)
    await tick(ctx3, at(FRI, 9, 40))
    await whole_day(ctx3, SAT)
    assert len(calls) == 1 and len(ah.calls) == n and ctx3.hass.notifications() == []


def test_old_or_broken_state_file_loads(tmp_path):
    ctx = make_ctx(tmp_path)
    (tmp_path / "state.json").write_text('{"schedule_done": "2026-10-25"}', encoding="utf-8")
    main.load_state(ctx)
    assert ctx.checked is None and ctx.waiting is None
    (tmp_path / "state.json").write_text("[1, 2]", encoding="utf-8")
    main.load_state(ctx)
    assert ctx.checked is None and ctx.waiting is None


async def test_target_week_moves_on_after_sunday(tmp_path, planned):
    calls, queue = planned
    nov11 = date(2026, 11, 11)
    queue[:] = [plan_a(), make_plan([dish(nov11, 301, [6], 1.0)])]
    ah = FakeAh(tmp_path, periods=[CUR, NXT])
    ctx = make_ctx(tmp_path, ah=ah)
    await tick(ctx, at(FRI, 8, 0))
    assert ctx.ledger.load(W).plan is not None
    ah.periods = [NXT]
    await whole_day(ctx, WED)  # delivery day: the target is the week of 11 November now
    await whole_day(ctx, date(2026, 11, 5))
    assert len(calls) == 1 and ctx.waiting == main.Waiting(nov11, date(2026, 11, 6))
    ah.periods = [NXT, NXT2]
    await tick(ctx, at(date(2026, 11, 6), 8, 0))
    assert len(calls) == 2 and ah.offers_for[-1] == NXT2
    assert ctx.ledger.load(nov11).plan is not None
    assert first_lines(ctx) == ["Voor de bestelling van woensdag 4 november",
                                "Voor de bestelling van woensdag 11 november"]


async def test_unreadable_settings_try_again_next_hour(tmp_path, planned):
    calls, _ = planned
    ctx = make_ctx(tmp_path)
    ctx.hass.fail_reads = True
    await tick(ctx, at(FRI, 8, 0))
    await tick(ctx, at(FRI, 8, 30))
    assert ctx.client.calls == [] and calls == [] and ctx.error
    assert "HA is weg" not in ctx.error
    ctx.hass.fail_reads = False
    await tick(ctx, at(FRI, 9, 0))
    assert len(calls) == 1 and ctx.error is None
    assert ctx.client.held() == {1: 1, 2: 2, 3: 1}


async def test_non_http_settings_error_tries_again_next_hour(tmp_path, planned):
    calls, _ = planned
    ctx = make_ctx(tmp_path)
    real = ctx.hass.read_settings

    async def broken():
        raise ValueError("bad helper value")

    ctx.hass.read_settings = broken
    await tick(ctx, at(FRI, 8, 0))
    assert "ValueError" in ctx.error and "bad helper value" not in ctx.error
    assert calls == []
    ctx.hass.read_settings = real
    await tick(ctx, at(FRI, 9, 0))
    assert len(calls) == 1


async def test_ah_read_error_before_writing_is_quiet_and_retried(tmp_path, planned):
    calls, _ = planned
    ah = FakeAh(tmp_path, periods=[CUR, NXT])
    ah.raise_on["bonus_offers"] = AhApiError("boom", 503)
    ctx = make_ctx(tmp_path, ah=ah)
    await tick(ctx, at(SAT, 8, 0))
    assert ctx.hass.notifications() == [] and ah.writes == [] and ctx.error
    assert ctx.ledger.load(W).plan is None
    del ah.raise_on["bonus_offers"]
    await tick(ctx, at(SAT, 9, 0))
    assert len(calls) == 1 and len(ctx.hass.notifications()) == 1 and ctx.error is None


async def test_expired_login_is_reported_once_not_every_hour(tmp_path, planned):
    ah = FakeAh(tmp_path, periods=[CUR, NXT])
    ah.raise_on["bonus_periods"] = AhLoginExpired("login expired", 401)
    ctx = make_ctx(tmp_path, ah=ah)
    await tick(ctx, at(FRI, 8, 0))
    await tick(ctx, at(FRI, 9, 0))
    await tick(ctx, at(FRI, 10, 0))
    assert [n["message"] for n in ctx.hass.notifications()] == [main.LOGIN_EXPIRED]
    assert ah.writes == []


async def test_disabled_addon_does_not_look_at_the_folder(tmp_path, planned):
    calls, _ = planned
    ctx = make_ctx(tmp_path, hass=FakeHass(tmp_path / "totaal.json", enabled=False))
    await whole_day(ctx, FRI)
    assert ctx.client.calls == [] and calls == []
    ctx.hass.states["input_boolean.weekmenu_aan"] = "on"  # switched on Saturday
    await tick(ctx, at(SAT, 11, 0))
    assert len(calls) == 1


def test_check_slots():
    t = main.check_slot
    at8, at830 = main.time(8, 0), main.time(8, 30)
    assert t(at(FRI, 7, 59), at8) is None
    assert t(at(FRI, 8, 0), at8) == at(FRI, 8, 0)
    assert t(at(FRI, 8, 59), at8) == at(FRI, 8, 0)
    assert t(at(FRI, 8, 15), at830) is None
    assert t(at(FRI, 8, 45), at830) == at(FRI, 8, 30)
    assert t(at(FRI, 9, 5), at830) == at(FRI, 9, 0)
    assert t(at(FRI, 18, 0), at8) == at(FRI, 18, 0)
    assert t(at(FRI, 23, 30), at8) == at(FRI, 18, 0)  # a late start still gets the last look
    assert t(at(FRI, 20, 0), main.time(19, 0)) == at(FRI, 19, 0)


# --- fix round 1 of the schedule change (Rulings 28-30) ------------------------------------


class Crash(BaseException):
    """The process dying halfway (not an Exception: nothing catches it)."""


async def test_crash_after_one_write_is_completed_by_the_next_slot(tmp_path, planned):
    calls, _ = planned
    # write 1 succeeds, write 2 reaches AH and then the process dies
    ah = FakeAh(tmp_path, periods=[CUR, NXT], fail_on_call=1, fail_after=True, exc=Crash())
    ctx = make_ctx(tmp_path, ah=ah)
    with pytest.raises(Crash):
        await tick(ctx, at(FRI, 8, 0))
    wlog = ctx.ledger.load(W)
    assert wlog.plan is not None and wlog.round_done is False and wlog.pending
    assert len(ah.writes) == 2 and ctx.hass.notifications() == []

    ah.fail_on_call = None
    ctx2 = make_ctx(tmp_path, ah=ah)  # restart
    main.load_state(ctx2)
    await tick(ctx2, at(FRI, 8, 20))
    assert len(calls) == 1  # same plan
    assert len(ah.writes) == 3  # only product 3; product 2 was settled from pending
    assert ah.held() == {1: 1, 2: 2, 3: 1}
    wlog = ctx2.ledger.load(W)
    assert wlog.pending == {} and wlog.round_done is True
    (note,) = ctx2.hass.notifications()
    assert note["message"].splitlines()[0] == "Voor de bestelling van woensdag 4 november"
    n = len(ah.calls)
    await whole_day(ctx2, SAT)
    assert len(ah.calls) == n and len(ctx2.hass.notifications()) == 1


async def test_completion_round_runs_only_once(tmp_path, planned):
    ah = FakeAh(tmp_path, periods=[CUR, NXT], fail_on_call=1, exc=Crash())
    ctx = make_ctx(tmp_path, ah=ah)
    with pytest.raises(Crash):
        await tick(ctx, at(FRI, 8, 0))
    ah.fail_on_call, ah.exc = 2, AhApiError("boom", 500)  # the completion fails as well
    ctx2 = make_ctx(tmp_path, ah=ah)
    await tick(ctx2, at(FRI, 9, 0))
    (note,) = ctx2.hass.notifications()
    assert note["title"] == "Weekmenu mislukt" and "Tik op Nu maken" in note["message"]
    n = len(ah.writes)
    await tick(ctx2, at(FRI, 10, 0))
    await whole_day(ctx2, SAT)
    assert len(ah.writes) == n and len(ctx2.hass.notifications()) == 1


async def test_round_done_means_nothing_more(tmp_path, planned):
    calls, _ = planned
    ctx = make_ctx(tmp_path)
    ctx.ledger.save(WeekLog(W, plan=plan_a().to_dict(), added={1: 1}, round_done=True))
    await whole_day(ctx, FRI)
    assert ctx.client.calls == [] and ctx.client.writes == [] and calls == []


def test_old_log_with_a_plan_counts_as_done():
    d = WeekLog(W, plan=plan_a().to_dict()).to_dict()
    del d["round_done"]
    assert WeekLog.from_dict(d).round_done is True
    d = WeekLog(W).to_dict()
    del d["round_done"]
    assert WeekLog.from_dict(d).round_done is False


async def test_fallback_day_retries_settings_until_2300_then_one_push(tmp_path, planned):
    calls, _ = planned
    ctx = make_ctx(tmp_path, ah=FakeAh(tmp_path, periods=[CUR]))
    ctx.hass.fail_reads = True
    for hh in range(17, 23):
        await tick(ctx, at(SUN, hh, 0))
        await tick(ctx, at(SUN, hh, 30))
    assert ctx.hass.notifications() == [] and calls == []
    await tick(ctx, at(SUN, 23, 0))
    await tick(ctx, at(SUN, 23, 45))
    (note,) = ctx.hass.notifications()
    assert note["message"] == (
        "Weekmenu kon niet gemaakt worden: Home Assistant/AH niet bereikbaar. "
        "Tik op Nu maken in de Weekmenu-app.")
    assert note["data"]["clickAction"] == ctx.ingress_url


async def test_fallback_day_settings_back_at_2000_makes_the_menu(tmp_path, planned):
    calls, _ = planned
    ctx = make_ctx(tmp_path, ah=FakeAh(tmp_path, periods=[CUR]))
    ctx.hass.fail_reads = True
    await tick(ctx, at(SUN, 18, 0))
    await tick(ctx, at(SUN, 19, 0))
    ctx.hass.fail_reads = False
    await tick(ctx, at(SUN, 20, 0))
    assert len(calls) == 1
    (note,) = ctx.hass.notifications()
    assert PERIOD_WARNING in note["message"]


async def test_final_failure_push_while_ha_is_down_is_logged(tmp_path, planned, caplog):
    ctx = make_ctx(tmp_path, ah=FakeAh(tmp_path, periods=[CUR]),
                   hass=FakeHass(tmp_path / "totaal.json", fail_posts=True))
    ctx.hass.fail_reads = True
    await tick(ctx, at(SUN, 23, 0))  # must not raise
    assert any("melding" in r.getMessage() for r in caplog.records)


async def test_stdin_button_before_folder_pushes_once_per_week(tmp_path, planned):
    ah = FakeAh(tmp_path, periods=[CUR])
    ctx = make_ctx(tmp_path, ah=ah, now=at(date(2026, 10, 29), 10, 0))
    out = await handle_command(ctx, '{"command": "nu_maken"}')
    assert out.status == "waiting_for_folder"
    await handle_command(ctx, '{"command": "nieuw_menu"}')
    (note,) = ctx.hass.notifications()
    assert note["message"] == ("De folder van volgende week is er nog niet, verwacht vanaf "
                               "vrijdag 30 oktober. Het menu komt dan vanzelf.")
    assert ah.writes == []
    ctx2 = make_ctx(tmp_path, ah=ah, now=at(date(2026, 10, 29), 11, 0))  # after a restart
    main.load_state(ctx2)
    await handle_command(ctx2, '{"command": "nu_maken"}')
    assert ctx2.hass.notifications() == []


async def test_waiting_keeps_an_unrelated_error(tmp_path, planned):
    ah = FakeAh(tmp_path, periods=[CUR, NXT], fail_on_call=0)
    ctx = make_ctx(tmp_path, ah=ah)
    await run_round(ctx, at(FRI, 10, 0), "nu_maken")  # a write failure for 4 November
    write_error = ctx.error
    assert write_error
    ah.fail_on_call = None
    ah.periods = [NXT]
    await run_round(ctx, at(MON, 10, 0), "nu_maken")  # next week: waiting
    assert ctx.error == write_error  # not cleared by an unrelated waiting


async def test_waiting_clears_an_earlier_read_error(tmp_path, planned):
    ctx = make_ctx(tmp_path, ah=FakeAh(tmp_path, periods=[CUR]))
    ctx.hass.fail_reads = True
    await tick(ctx, at(FRI, 8, 0))
    assert ctx.error
    ctx.hass.fail_reads = False
    await tick(ctx, at(FRI, 9, 0))
    assert ctx.error is None and ctx.waiting is not None


# --- fix round 2 of the schedule change ---------------------------------------------------


async def test_expired_login_does_not_use_up_the_completion(tmp_path, planned):
    ah = FakeAh(tmp_path, periods=[CUR, NXT], fail_on_call=1, exc=Crash())
    ctx = make_ctx(tmp_path, ah=ah)
    with pytest.raises(Crash):
        await tick(ctx, at(FRI, 8, 0))
    ah.fail_on_call = None
    ah.raise_on["active_order"] = AhLoginExpired("login expired", 401)
    ctx2 = make_ctx(tmp_path, ah=ah)  # restart, and the login has expired meanwhile
    await tick(ctx2, at(FRI, 9, 0))
    (note,) = ctx2.hass.notifications()
    assert note["message"] == main.LOGIN_EXPIRED_COMPLETION
    assert "Log opnieuw in via de Weekmenu-app; daarna wordt het menu vanzelf afgemaakt." \
        in note["message"]
    wlog = ctx2.ledger.load(W)
    assert wlog.completion_tried is False and wlog.round_done is False
    assert len(ah.writes) == 2
    # logged in again (api/login clears the flag): the next slot completes the week
    del ah.raise_on["active_order"]
    ctx2.login_expired = False
    await tick(ctx2, at(FRI, 10, 0))
    assert ah.held() == {1: 1, 2: 2, 3: 1}
    assert ctx2.ledger.load(W).round_done is True
    assert len(ctx2.hass.notifications()) == 2  # the login message, then "klaar"


async def test_crash_during_nieuw_menu_is_completed_by_the_next_slot(tmp_path, planned):
    calls, queue = planned
    queue[:] = [plan_a(), plan_b()]
    ah = FakeAh(tmp_path, periods=[CUR, NXT])
    ctx = make_ctx(tmp_path, ah=ah)
    await run_round(ctx, at(FRI, 10, 0), "nu_maken")
    assert ctx.ledger.load(W).round_done is True and len(ah.writes) == 3
    ah.fail_on_call, ah.exc = 4, Crash()  # plan_b adds 4 and 5; dies writing 5
    with pytest.raises(Crash):
        await run_round(ctx, at(FRI, 11, 0), "nieuw_menu")
    wlog = ctx.ledger.load(W)
    assert wlog.round_done is False and wlog.completion_tried is False
    assert Plan.from_dict(wlog.plan).dishes[0].recipe.id == 201  # the new menu
    ah.fail_on_call = None
    ctx2 = make_ctx(tmp_path, ah=ah)
    await tick(ctx2, at(FRI, 12, 0))
    assert ah.held() == {1: 1, 2: 2, 3: 1, 4: 1, 5: 1}
    assert len(calls) == 2  # completed from the log, not a third menu
    assert ctx2.ledger.load(W).round_done is True
    assert len(ctx2.hass.notifications()) == 1


async def test_crash_after_the_message_sends_no_second_message(tmp_path, planned, monkeypatch):
    ah = FakeAh(tmp_path, periods=[CUR, NXT])
    ctx = make_ctx(tmp_path, ah=ah)
    real_save = ctx.ledger.save

    def dies_on_round_done(log):
        if log.round_done:
            raise Crash()
        real_save(log)

    monkeypatch.setattr(ctx.ledger, "save", dies_on_round_done)
    with pytest.raises(Crash):
        await tick(ctx, at(FRI, 8, 0))
    assert len(ctx.hass.notifications()) == 1
    wlog = Ledger(tmp_path / "weeks").load(W)
    assert wlog.notified is True and wlog.round_done is False
    ctx2 = make_ctx(tmp_path, ah=ah)  # restart
    n = len(ah.writes)
    await tick(ctx2, at(FRI, 9, 0))
    assert ctx2.hass.notifications() == [] and len(ah.writes) == n
    assert ctx2.ledger.load(W).round_done is True



# --- 0 personen = niet thuis eten ---------------------------------------------------------


def names(days):
    return [main.WEEKDAGEN[d.weekday()][:2] for d in days]


async def test_zero_persons_on_friday_is_skipped_in_the_settings(tmp_path, planned):
    calls, _ = planned
    hass = FakeHass(tmp_path / "totaal.json", aantal=5)
    hass.states["input_number.weekmenu_personen_vrijdag"] = "0"
    ctx = make_ctx(tmp_path, hass=hass)
    await run_round(ctx, SUNDAY, "nu_maken")
    s = calls[0]
    assert names(s.evenings) == ["wo", "do", "za", "zo", "ma"]
    assert VR not in s.persons and all(n >= 1 for n in s.persons.values())


async def test_zero_on_friday_and_saturday_shifts_five_evenings(tmp_path, planned):
    calls, _ = planned
    hass = FakeHass(tmp_path / "totaal.json", aantal=5)
    hass.states["input_number.weekmenu_personen_zaterdag"] = "0"
    hass.states["input_number.weekmenu_personen_vrijdag"] = "0"
    ctx = make_ctx(tmp_path, hass=hass)
    await run_round(ctx, SUNDAY, "nu_maken")
    assert names(calls[0].evenings) == ["wo", "do", "zo", "ma", "di"]


async def test_message_and_sensor_name_the_evening_not_at_home(tmp_path, planned):
    hass = FakeHass(tmp_path / "totaal.json", aantal=5)
    hass.states["input_number.weekmenu_personen_vrijdag"] = "0"
    ctx = make_ctx(tmp_path, hass=hass)
    await run_round(ctx, SUNDAY, "nu_maken")
    assert "vr niet thuis" in hass.notifications()[0]["message"]
    sensor = next(p for path, p in hass.posts if path == "states/sensor.weekmenu")
    assert sensor["attributes"]["niet_thuis"] == ["2026-11-06"]


async def test_all_days_zero_makes_no_menu_and_says_so(tmp_path, planned):
    calls, _ = planned
    hass = FakeHass(tmp_path / "totaal.json", aantal=5)
    for eid in helper_persons().values():
        hass.states[eid] = "0"
    ctx = make_ctx(tmp_path, hass=hass)
    out = await run_round(ctx, SUNDAY, "nu_maken")
    assert out.status == "skipped" and calls == []
    assert ctx.ledger.load(W).plan is None
    [note] = hass.notifications()
    assert "niet thuis" in note["message"] and "geen menu" in note["message"].lower()
    assert "—" not in note["message"] and "–" not in note["message"]
    assert ctx.error is None


async def test_all_days_zero_tells_the_schedule_once_a_week(tmp_path, planned):
    calls, _ = planned
    hass = FakeHass(tmp_path / "totaal.json", aantal=5)
    for eid in helper_persons().values():
        hass.states[eid] = "0"
    ctx = make_ctx(tmp_path, hass=hass)
    await run_round(ctx, at(date(2026, 11, 6), 9), "schema")
    await run_round(ctx, at(date(2026, 11, 6), 10), "schema")
    assert calls == [] and len(hass.notifications()) == 1


async def test_old_state_and_log_with_parents_fields_still_load(tmp_path):
    ctx = make_ctx(tmp_path)
    ctx.state_path.write_text(json.dumps(
        {"checked": "2026-11-01T08:00", "ouders": "vrijdag", "waiting": None}), encoding="utf-8")
    main.load_state(ctx)
    assert ctx.checked == datetime(2026, 11, 1, 8, 0)
    plan = make_plan([dish(W, 1, [1])]).to_dict()
    plan["ouders"] = "vrijdag"
    old = WeekLog(W, plan=plan).to_dict()
    old["ouders"] = "vrijdag"
    (tmp_path / "weeks").mkdir(exist_ok=True)
    (tmp_path / "weeks" / f"{W.isoformat()}.json").write_text(json.dumps(old), encoding="utf-8")
    loaded = ctx.ledger.load(W)
    assert loaded.week == W and len(Plan.from_dict(loaded.plan).dishes) == 1


def test_no_parents_text_anywhere_in_the_app():
    from pathlib import Path
    root = Path(main.__file__).parent
    for path in [*root.glob("*.py"), *(root / "static").glob("*")]:
        if path.suffix in {".py", ".js", ".html", ".css"}:
            assert "ouder" not in path.read_text(encoding="utf-8").lower(), path.name
