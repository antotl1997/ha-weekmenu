import json
from datetime import date, datetime, timedelta

import pytest

from app import main
from app.ahclient import AhApiError, AhLoginExpired, AhNotJson, BonusPeriod
from app.ledger import Ledger, WeekLog, new_folder_available
from app.planner import Plan
from app.hass import helper_persons
from app.main import (
    PERIOD_WARNING,
    WRITE_FAILED_HINT,
    complete_round,
    handle_command,
    run_round,
    tick,
    update_total,
)
from app.tokens import Tokens
from app.week import menu_week_start
from app.writer import apply
from tests.fakes import NEXT, SUNDAY, W, FakeAh, FakeHass, dish, make_ctx, make_plan

WO, DO, VR = W, date(2026, 11, 5), date(2026, 11, 6)


def plan_a():
    return make_plan([dish(WO, 101, [1, 2], 2.0), dish(DO, 102, [2, 3], 3.0)])


def plan_b():
    return make_plan([dish(WO, 201, [1, 4], 1.0), dish(VR, 202, [5], 2.0)])


@pytest.fixture
def rebuilt(monkeypatch):
    """rebuild_plan (Menu bijwerken) returns the queued plans in turn, and records the
    menu it was asked to rebuild."""
    calls = []
    queue = [plan_b()]

    async def fake_rebuild(client, plan, bonus, overrides=None):
        calls.append(plan)
        return queue.pop(0) if len(queue) > 1 else queue[0]

    monkeypatch.setattr(main, "rebuild_plan", fake_rebuild)
    return calls, queue


def confirmed(ctx, plan=None, start=W, **fields):
    """A menu confirmed in the app whose round did not end yet (nothing written): the
    hourly look completes it from the log."""
    ctx.ledger.save(WeekLog(start, plan=(plan or plan_a()).to_dict(),
                            folder_period_start=NEXT.start, **fields))


async def confirm(ctx, now, plan=None):
    """What "Zet op lijst" (web.confirm) does with the log: the menu written once
    through apply; round_done only when nothing is held. Its message is the route's."""
    wlog = ctx.ledger.load(menu_week_start(now))
    wlog.folder_period_start = NEXT.start
    result = await apply(ctx.client, ctx.ledger, wlog, plan or plan_a(), None,
                         today=now.date())
    wlog.notified = True
    wlog.round_done = result.held is None
    ctx.ledger.save(wlog)
    return result


def login(ctx):
    ctx.store.save(Tokens("test-access", "test-refresh", 9e9))


# --- the brief's tests -------------------------------------------------------------


async def test_completion_writes_only_what_the_log_does_not_have(tmp_path):
    ctx = make_ctx(tmp_path)
    confirmed(ctx, added={1: 1})
    out = await complete_round(ctx, SUNDAY)
    assert out.status == "done"
    # the menu does not read the folder; only the staples step after it does
    assert "bonus_offers" not in ctx.client.calls
    assert ctx.client.calls == ["bonus_periods", "previously_bought_bonus", "purchases"]
    assert ctx.client.held() == {2: 2, 3: 1}  # only what the log does not have yet
    assert ctx.ledger.load(W).round_done is True


async def test_login_expired_notifies_and_writes_nothing(tmp_path, rebuilt):
    ah = FakeAh(tmp_path, periods=[CUR, NXT])
    ah.raise_on["bonus_offers"] = AhLoginExpired("login expired", 401)
    ctx = make_ctx(tmp_path, ah=ah, ingress_url="/hassio/ingress/abc_weekmenu")
    early(ctx, ah, new_folder=NXT.start)
    n = len(ah.writes)
    out = await run_round(ctx, SUNDAY, "bijwerken")
    assert out.status == "login_expired"
    assert len(ah.writes) == n
    (note,) = ctx.hass.notifications()
    assert note["message"] == "AH-login verlopen, log opnieuw in"
    assert note["data"]["clickAction"] == "/hassio/ingress/abc_weekmenu"
    assert not any(path == "states/sensor.weekmenu" for path, _ in ctx.hass.posts)
    assert ctx.login_expired and ctx.error


async def test_restart_mid_write_then_the_hourly_look_adds_no_duplicates(tmp_path, rebuilt):
    calls, _ = rebuilt
    # Menu bijwerken: the 2nd write reaches AH, then the connection drops
    ah = FakeAh(tmp_path, periods=[CUR, NXT], fail_on_call=1, fail_after=True)
    ctx = make_ctx(tmp_path, ah=ah)
    early(ctx, ah, new_folder=NXT.start)
    out = await run_round(ctx, SUNDAY, "bijwerken")
    assert out.status == "error"
    note = ctx.hass.notifications()[0]["message"]
    assert "boom" in note and WRITE_FAILED_HINT in note and "Nu maken" not in note
    assert "scherm Week" in note
    assert len(ah.writes) == 2  # stopped, nothing retried
    assert ctx.ledger.load(W).round_done is False

    # "restart": a fresh context on the same /data, same AH state
    ah.fail_on_call = None
    ctx2 = make_ctx(tmp_path, ah=ah)
    ctx2.ledger = Ledger(tmp_path / "weeks")
    # the round did not finish: the next hourly look completes it, once
    await tick(ctx2, datetime(2026, 11, 1, 9, 0))
    assert len(calls) == 1  # the plan in the log is completed, not rebuilt
    # the completion only adds; what the old menu needed stays and is reported
    assert ah.held() == {1: 1, 2: 2, 3: 1, 4: 1, 5: 1}
    # the write AH had applied was settled, not repeated: every product written once
    assert ah.writes == [("list", {4: 1}), ("list", {5: 1})]
    assert ctx2.ledger.load(W).round_done is True
    (note,) = ctx2.hass.notifications()  # the "klaar" message, once
    assert "Over van het vorige menu" in note["message"]
    out = await complete_round(ctx2, datetime(2026, 11, 1, 10, 0))
    assert out.status == "skipped"
    await tick(ctx2, datetime(2026, 11, 1, 10, 0))
    assert len(ah.writes) == 2


async def test_week_counted_once_in_total(tmp_path):
    ctx = make_ctx(tmp_path)
    (tmp_path / "totaal.json").write_text(json.dumps({"total": 10.0}), encoding="utf-8")
    ctx.ledger.save(WeekLog(date(2026, 10, 28), plan=plan_b().to_dict(), savings=4.0))
    confirmed(ctx)
    await complete_round(ctx, SUNDAY)
    await run_round(ctx, SUNDAY, "bijwerken")  # nothing to update: still counts the total
    await update_total(ctx, SUNDAY.date())
    assert ctx.hass.totals() == [14.0, 14.0, 14.0]
    assert json.loads((tmp_path / "totaal.json").read_text())["total"] == 14.0
    assert ctx.ledger.load(date(2026, 10, 28)).counted
    assert not ctx.ledger.load(W).counted  # starts on Wednesday, counted then


# --- extra coverage ----------------------------------------------------------------


async def test_failing_home_assistant_never_breaks_the_round(tmp_path):
    ctx = make_ctx(tmp_path, hass=FakeHass(tmp_path / "totaal.json", fail_posts=True))
    confirmed(ctx)
    out = await complete_round(ctx, SUNDAY)
    assert out.status == "done" and ctx.client.held() == {1: 1, 2: 2, 3: 1}


async def test_ah_html_response_notifies_and_writes_nothing(tmp_path, rebuilt):
    ah = FakeAh(tmp_path, periods=[CUR, NXT])
    ah.raise_on["bonus_offers"] = AhNotJson("GET /x: response is not JSON", 403)
    ctx = make_ctx(tmp_path, ah=ah)
    early(ctx, ah, new_folder=NXT.start)
    before = ctx.ledger.load(W).plan
    out = await run_round(ctx, SUNDAY, "bijwerken")
    assert out.status == "error" and ah.writes == []
    assert "not JSON" in ctx.hass.notifications()[0]["message"]
    assert ctx.ledger.load(W).plan == before


async def test_settings_follow_helpers(tmp_path):
    hass = FakeHass(tmp_path / "totaal.json", aantal=3)
    hass.states["input_number.weekmenu_personen_donderdag"] = "0"
    hass.states["input_number.weekmenu_personen_vrijdag"] = "4"
    ctx = make_ctx(tmp_path, hass=hass)
    s = await main.week_settings(ctx, W)
    # 6 eat days, 3 cooking moments: every cook day has a leftover night
    za, zo, ma, di = (date(2026, 11, d) for d in (7, 8, 9, 10))
    assert s.evenings == [WO, za, ma]
    assert s.leftovers == {WO: VR, za: zo, ma: di}
    assert s.persons == {WO: 2 + 4, za: 4, ma: 4}
    assert s.pantry == ctx.pantry


async def test_stdin_commands(tmp_path):
    ctx = make_ctx(tmp_path)
    out = await handle_command(ctx, '{"command": "bijwerken"}\n')
    assert out.status == "skipped"  # no menu with an older folder
    assert await handle_command(ctx, '{"command": "rm -rf"}') is None
    assert await handle_command(ctx, "niet eens json") is None
    assert await handle_command(ctx, "") is None


# --- fix round 1 (Ruling 22) -------------------------------------------------------


async def test_total_not_counted_twice_after_crash_between_file_and_flags(
    tmp_path, monkeypatch
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
    assert data == {"schema": 1, "total": 14.0, "counted": {"2026-10-28": 4.0}}
    assert not Ledger(tmp_path / "weeks").load(date(2026, 10, 28)).counted  # flag lost

    monkeypatch.setattr(ctx.ledger, "save", real_save)
    await update_total(ctx, SUNDAY.date())
    assert json.loads((tmp_path / "totaal.json").read_text())["total"] == 14.0
    assert ctx.hass.totals()[-1] == 14.0


@pytest.mark.parametrize("old", ["12.5", '{"total": 12.5}'])
async def test_old_total_file_is_migrated_from_ledger_flags(tmp_path, old):
    ctx = make_ctx(tmp_path)
    (tmp_path / "totaal.json").write_text(old, encoding="utf-8")
    ctx.ledger.save(WeekLog(date(2026, 10, 21), plan=plan_b().to_dict(), savings=3.0,
                            counted=True))
    ctx.ledger.save(WeekLog(date(2026, 10, 28), plan=plan_b().to_dict(), savings=4.0))
    await update_total(ctx, SUNDAY.date())
    data = json.loads((tmp_path / "totaal.json").read_text())
    assert data == {"schema": 1, "total": 16.5, "counted": {"2026-10-21": 3.0, "2026-10-28": 4.0}}


async def test_completion_adding_nothing_sends_no_second_message(tmp_path):
    ctx = make_ctx(tmp_path)
    ctx.client.listed = dict(plan_a().basket)
    confirmed(ctx, added=dict(plan_a().basket), added_to="list", notified=True)
    out = await complete_round(ctx, SUNDAY)
    assert out.status == "done" and out.result.added == {}
    assert ctx.hass.notifications() == []  # no second "klaar"
    assert sum(p == "states/sensor.weekmenu" for p, _ in ctx.hass.posts) == 1  # published
    assert ctx.ledger.load(W).round_done is True
    # and nothing at all after that
    out = await complete_round(ctx, datetime(2026, 11, 1, 10, 0))
    assert out.status == "skipped"
    assert sum(p == "states/sensor.weekmenu" for p, _ in ctx.hass.posts) == 1


def test_weeklog_without_period_warning_loads():
    d = WeekLog(W).to_dict()
    del d["period_warning"]
    assert WeekLog.from_dict(d).period_warning is None
    log = WeekLog(W, period_warning="x")
    assert WeekLog.from_dict(log.to_dict()).period_warning == "x"


async def test_old_counted_list_is_migrated_with_ledger_values(tmp_path):
    ctx = make_ctx(tmp_path)
    (tmp_path / "totaal.json").write_text('{"total": 3.0, "counted": ["2026-10-21"]}',
                                          encoding="utf-8")
    ctx.ledger.save(WeekLog(date(2026, 10, 21), plan=plan_b().to_dict(), savings=3.0,
                            extras_savings=0.5, counted=True))
    await update_total(ctx, SUNDAY.date())
    data = json.loads((tmp_path / "totaal.json").read_text())
    # the list had no amounts: the ledger value at migration time counts as counted
    assert data == {"schema": 1, "total": 3.0, "counted": {"2026-10-21": 3.5}}


async def test_staple_added_after_week_was_counted_reaches_total_once(tmp_path):
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
    assert data == {"schema": 1, "total": 5.25, "counted": {"2026-10-28": 5.25}}


async def test_total_never_goes_down(tmp_path):
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


async def test_refused_refresh_in_a_round_asks_for_login(tmp_path, rebuilt, respx_mock,
                                                         aiohttp_client):
    import time as _time

    import httpx

    from app.ahclient import AhClient
    from app.web import make_app

    respx_mock.post("https://api.ah.nl/mobile-auth/v1/auth/token/refresh").respond(
        400, json={"error": "invalid_grant"})
    ah = FakeAh(tmp_path)
    ah.store.save(Tokens("a", "r", _time.time() + 10))  # about to expire
    async with httpx.AsyncClient() as http:
        real = AhClient(ah.store, http)
        ah.bonus_periods = real.bonus_periods  # the first AH call refreshes the token
        ctx = make_ctx(tmp_path, ah=ah)
        early(ctx, ah, new_folder=NXT.start)
        n = len(ah.writes)
        out = await run_round(ctx, SUNDAY, "bijwerken")
    assert out.status == "login_expired" and len(ah.writes) == n
    (note,) = ctx.hass.notifications()
    assert note["message"] == "AH-login verlopen, log opnieuw in"
    assert ctx.login_expired
    client = await aiohttp_client(make_app(ctx))
    status = await (await client.get("/api/status")).json()
    assert status["logged_in"] is False  # app.js shows the login screen


# --- schedule: the hourly look (v1.1: never a menu) ---------------------------------------

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


async def test_friday_0800_without_folder_does_nothing(tmp_path):
    ah = FakeAh(tmp_path, periods=[CUR])
    ctx = make_ctx(tmp_path, ah=ah)
    login(ctx)
    await tick(ctx, at(FRI, 7, 30))
    assert ah.calls == []  # before schedule_time: not even a look
    await tick(ctx, at(FRI, 8, 0))
    assert ah.calls == ["bonus_periods"]  # one look, no bonus_offers
    await tick(ctx, at(FRI, 8, 30))
    assert ah.calls == ["bonus_periods"]  # at most once per hour
    assert ah.writes == [] and ctx.hass.notifications() == []
    assert ctx.ledger.load(W).plan is None
    assert ctx.waiting == main.Waiting(W, date(2026, 10, 30))
    assert ctx.error is None and ctx.last_round is None


async def test_friday_0900_folder_out_fills_the_choice_list_not_a_menu(tmp_path):
    ah = FakeAh(tmp_path, periods=[CUR])
    ctx = make_ctx(tmp_path, ah=ah)
    login(ctx)
    await tick(ctx, at(FRI, 8, 0))
    ah.periods = [CUR, NXT]  # the folder appears
    await tick(ctx, at(FRI, 9, 0))
    assert ah.offers_for == [NXT]  # read for the choice list
    assert ctx.ledger.load(W).plan is None and ah.writes == []
    assert ctx.hass.notifications() == [] and ctx.waiting is None
    assert ctx.last_round is None and ctx.rounds == 0


async def test_restart_while_waiting_looks_once_per_hour(tmp_path):
    ah = FakeAh(tmp_path, periods=[CUR])
    ctx = make_ctx(tmp_path, ah=ah)
    login(ctx)
    await tick(ctx, at(FRI, 8, 0))
    assert ah.calls == ["bonus_periods"]
    ctx2 = make_ctx(tmp_path, ah=ah)  # restart at 08:20
    main.load_state(ctx2)
    assert ctx2.waiting == main.Waiting(W, date(2026, 10, 30))
    await tick(ctx2, at(FRI, 8, 20))
    assert ah.calls == ["bonus_periods"]  # this hour was already looked at
    ah.periods = [CUR, NXT]
    await tick(ctx2, at(FRI, 9, 0))
    assert ah.offers_for == [NXT] and ctx2.waiting is None
    assert ctx2.ledger.load(W).plan is None and ctx2.hass.notifications() == []


def test_old_or_broken_state_file_loads(tmp_path):
    ctx = make_ctx(tmp_path)
    (tmp_path / "state.json").write_text('{"schedule_done": "2026-10-25"}', encoding="utf-8")
    main.load_state(ctx)
    assert ctx.checked is None and ctx.waiting is None
    (tmp_path / "state.json").write_text("[1, 2]", encoding="utf-8")
    main.load_state(ctx)
    assert ctx.checked is None and ctx.waiting is None


async def test_target_week_moves_on_after_sunday(tmp_path):
    nov11 = date(2026, 11, 11)
    ah = FakeAh(tmp_path, periods=[NXT])
    ctx = make_ctx(tmp_path, ah=ah)
    login(ctx)
    await whole_day(ctx, WED)  # delivery day: the target is the week of 11 November now
    await whole_day(ctx, date(2026, 11, 5))
    assert ctx.waiting == main.Waiting(nov11, date(2026, 11, 6))
    ah.periods = [NXT, NXT2]
    await tick(ctx, at(date(2026, 11, 6), 8, 0))
    assert ah.offers_for[-1] == NXT2 and ctx.waiting is None
    assert ctx.ledger.load(nov11).plan is None and ctx.hass.notifications() == []


async def test_unreadable_settings_try_the_completion_again_next_hour(tmp_path):
    ctx = make_ctx(tmp_path)
    confirmed(ctx)
    ctx.hass.fail_reads = True
    await tick(ctx, at(FRI, 8, 0))
    await tick(ctx, at(FRI, 8, 30))
    assert ctx.client.writes == [] and ctx.error
    assert "HA is weg" not in ctx.error
    assert ctx.ledger.load(W).completion_tried is False  # not a real try
    ctx.hass.fail_reads = False
    await tick(ctx, at(FRI, 9, 0))
    assert ctx.error is None
    assert ctx.client.held() == {1: 1, 2: 2, 3: 1}


async def test_non_http_settings_error_tries_again_next_hour(tmp_path):
    ctx = make_ctx(tmp_path)
    confirmed(ctx)
    real = ctx.hass.read_settings

    async def broken():
        raise ValueError("bad helper value")

    ctx.hass.read_settings = broken
    await tick(ctx, at(FRI, 8, 0))
    assert "ValueError" in ctx.error and "bad helper value" not in ctx.error
    assert ctx.client.writes == []
    ctx.hass.read_settings = real
    await tick(ctx, at(FRI, 9, 0))
    assert ctx.client.held() == {1: 1, 2: 2, 3: 1}


async def test_expired_login_in_the_hourly_look_is_noted_and_pushed_once(tmp_path):
    ah = FakeAh(tmp_path, periods=[CUR, NXT])
    ah.raise_on["bonus_periods"] = AhLoginExpired("login expired", 401)
    ctx = make_ctx(tmp_path, ah=ah)
    login(ctx)
    await tick(ctx, at(FRI, 8, 0))
    await tick(ctx, at(FRI, 9, 0))
    await tick(ctx, at(FRI, 10, 0))
    assert ctx.login_expired  # the app and the dashboard card show it
    assert ah.calls == ["bonus_periods"]  # not tried again with an expired login
    # final review: the one "AH-login verlopen" push, on the first look that found it
    assert [n["message"] for n in ctx.hass.notifications()] == [main.LOGIN_EXPIRED]
    assert ah.writes == []


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


async def test_crash_after_one_write_is_completed_by_the_next_slot(tmp_path):
    # confirming: write 1 succeeds, write 2 reaches AH and then the process dies
    ah = FakeAh(tmp_path, periods=[CUR, NXT], fail_on_call=1, fail_after=True, exc=Crash())
    ctx = make_ctx(tmp_path, ah=ah)
    with pytest.raises(Crash):
        await confirm(ctx, at(FRI, 7, 0))
    wlog = ctx.ledger.load(W)
    assert wlog.plan is not None and wlog.round_done is False and wlog.pending
    assert len(ah.writes) == 2 and ctx.hass.notifications() == []

    ah.fail_on_call = None
    ctx2 = make_ctx(tmp_path, ah=ah)  # restart
    main.load_state(ctx2)
    await tick(ctx2, at(FRI, 8, 20))
    assert len(ah.writes) == 3  # only product 3; product 2 was settled from pending
    assert ah.held() == {1: 1, 2: 2, 3: 1}
    wlog = ctx2.ledger.load(W)
    assert wlog.pending == {} and wlog.round_done is True
    (note,) = ctx2.hass.notifications()
    assert note["message"].splitlines()[0] == "Voor de bestelling van woensdag 4 november"
    n = len(ah.calls)
    await whole_day(ctx2, SAT)
    assert len(ah.calls) == n and len(ctx2.hass.notifications()) == 1


async def test_completion_round_runs_only_once(tmp_path):
    ah = FakeAh(tmp_path, periods=[CUR, NXT], fail_on_call=1, exc=Crash())
    ctx = make_ctx(tmp_path, ah=ah)
    with pytest.raises(Crash):
        await confirm(ctx, at(FRI, 7, 0))
    ah.fail_on_call, ah.exc = 2, AhApiError("boom", 500)  # the completion fails as well
    ctx2 = make_ctx(tmp_path, ah=ah)
    await tick(ctx2, at(FRI, 9, 0))
    (note,) = ctx2.hass.notifications()
    assert note["title"] == "Weekmenu mislukt" and WRITE_FAILED_HINT in note["message"]
    n = len(ah.writes)
    await tick(ctx2, at(FRI, 10, 0))
    await whole_day(ctx2, SAT)
    assert len(ah.writes) == n and len(ctx2.hass.notifications()) == 1


async def test_round_done_means_nothing_more(tmp_path):
    ctx = make_ctx(tmp_path)
    ctx.ledger.save(WeekLog(W, plan=plan_a().to_dict(), added={1: 1}, round_done=True))
    await whole_day(ctx, FRI)
    assert ctx.client.calls == [] and ctx.client.writes == []


def test_old_log_with_a_plan_counts_as_done():
    d = WeekLog(W, plan=plan_a().to_dict()).to_dict()
    del d["round_done"]
    assert WeekLog.from_dict(d).round_done is True
    d = WeekLog(W).to_dict()
    del d["round_done"]
    assert WeekLog.from_dict(d).round_done is False


# --- fix round 2 of the schedule change ---------------------------------------------------


async def test_expired_login_does_not_use_up_the_completion(tmp_path):
    ah = FakeAh(tmp_path, periods=[CUR, NXT], fail_on_call=1, exc=Crash())
    ctx = make_ctx(tmp_path, ah=ah)
    with pytest.raises(Crash):
        await confirm(ctx, at(FRI, 7, 0))
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


async def test_crash_during_bijwerken_is_completed_by_the_next_slot(tmp_path, rebuilt):
    calls, _ = rebuilt
    ah = FakeAh(tmp_path, periods=[CUR, NXT])
    ctx = make_ctx(tmp_path, ah=ah)
    early(ctx, ah, new_folder=NXT.start)
    ah.fail_on_call, ah.exc = 1, Crash()  # dies on the second write of the new menu
    with pytest.raises(Crash):
        await run_round(ctx, at(FRI, 11, 0), "bijwerken")
    wlog = ctx.ledger.load(W)
    assert wlog.round_done is False and wlog.completion_tried is False
    assert Plan.from_dict(wlog.plan).dishes[0].recipe.id == 201  # the new menu
    ah.fail_on_call = None
    ctx2 = make_ctx(tmp_path, ah=ah)
    await tick(ctx2, at(FRI, 12, 0))
    # the completion only adds; what the old menu needed stays and is reported
    assert ah.held() == {1: 1, 2: 2, 3: 1, 4: 1, 5: 1}
    assert len(calls) == 1  # completed from the log, not rebuilt again
    assert ctx2.ledger.load(W).round_done is True
    assert len(ctx2.hass.notifications()) == 1


async def test_crash_after_the_message_sends_no_second_message(tmp_path, rebuilt,
                                                             monkeypatch):
    ah = FakeAh(tmp_path, periods=[CUR, NXT])
    ctx = make_ctx(tmp_path, ah=ah)
    early(ctx, ah, new_folder=NXT.start)
    real_save = ctx.ledger.save

    def dies_on_round_done(log):
        if log.round_done:
            raise Crash()
        real_save(log)

    monkeypatch.setattr(ctx.ledger, "save", dies_on_round_done)
    with pytest.raises(Crash):
        await run_round(ctx, at(FRI, 8, 0), "bijwerken")
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


async def test_zero_persons_on_friday_is_skipped_in_the_settings(tmp_path):
    hass = FakeHass(tmp_path / "totaal.json", aantal=5)
    hass.states["input_number.weekmenu_personen_vrijdag"] = "0"
    ctx = make_ctx(tmp_path, hass=hass)
    s = await main.week_settings(ctx, W)
    # 6 eat days, 5 cooking moments: one leftover night, the day after Wednesday
    assert names(s.evenings) == ["wo", "za", "zo", "ma", "di"]
    assert names(s.leftovers.values()) == ["do"]
    assert VR not in s.persons and all(n >= 1 for n in s.persons.values())


async def test_zero_on_friday_and_saturday_shifts_five_evenings(tmp_path):
    hass = FakeHass(tmp_path / "totaal.json", aantal=5)
    hass.states["input_number.weekmenu_personen_zaterdag"] = "0"
    hass.states["input_number.weekmenu_personen_vrijdag"] = "0"
    ctx = make_ctx(tmp_path, hass=hass)
    assert names((await main.week_settings(ctx, W)).evenings) == ["wo", "do", "zo", "ma", "di"]


async def test_message_and_sensor_name_the_evening_not_at_home(tmp_path):
    hass = FakeHass(tmp_path / "totaal.json", aantal=5)
    hass.states["input_number.weekmenu_personen_vrijdag"] = "0"
    ctx = make_ctx(tmp_path, hass=hass)
    confirmed(ctx)
    await complete_round(ctx, SUNDAY)
    assert "vr niet thuis" in hass.notifications()[0]["message"]
    sensor = next(p for path, p in hass.posts if path == "states/sensor.weekmenu")
    assert sensor["attributes"]["niet_thuis"] == ["2026-11-06"]


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
    from app.durable import UPDATE_MESSAGE
    root = Path(main.__file__).parent
    for path in [*root.glob("*.py"), *(root / "static").glob("*")]:
        if path.suffix in {".py", ".js", ".html", ".css"}:
            # the one allowed "ouder": the add-on is older than its data (v0.6)
            text = path.read_text(encoding="utf-8").replace(UPDATE_MESSAGE, "").lower()
            assert "ouder" not in text, path.name


# --- kookmomenten en restjes (v0.4) ----------------------------------------------------


def only_eat(hass, *dagen):
    for dag, eid in helper_persons().items():
        hass.states[eid] = "2" if dag in dagen else "0"


async def test_three_eat_days_two_cooking_moments_give_one_leftover_night(tmp_path):
    hass = FakeHass(tmp_path / "totaal.json", aantal=2)
    only_eat(hass, "woensdag", "donderdag", "vrijdag")
    hass.states["input_number.weekmenu_personen_donderdag"] = "3"
    ctx = make_ctx(tmp_path, hass=hass)
    s = await main.week_settings(ctx, W)
    assert s.evenings == [WO, VR]
    assert s.leftovers == {WO: DO}  # the day after the first cook day
    assert s.persons == {WO: 5, VR: 2}  # the cook day cooks for both days


async def test_more_cooking_moments_than_eat_days_is_bounded(tmp_path):
    hass = FakeHass(tmp_path / "totaal.json", aantal=7)
    only_eat(hass, "woensdag", "donderdag", "vrijdag")
    ctx = make_ctx(tmp_path, hass=hass)
    s = await main.week_settings(ctx, W)
    assert s.evenings == [WO, DO, VR] and s.leftovers == {}


# --- v0.4 C: menu before the new folder (v1.1: confirmed in the app) ---------------------

NEW_FOLDER_TEXT = "De nieuwe folder is er. Open de Weekmenu-app om je menu bij te werken."


def weekmenu_posts(ctx):
    return [p for path, p in ctx.hass.posts if path == "states/sensor.weekmenu"]


def early(ctx, ah, plan=None, **fields):
    """A menu confirmed and written with the current folder before next week's was out
    (the warning, folder_period_start the current period)."""
    plan = plan if plan is not None else plan_a()
    ah.listed = dict(plan.basket)
    ctx.ledger.save(WeekLog(W, plan=plan.to_dict(), added=dict(plan.basket), target="list",
                            savings=plan.savings,
                            added_to="list", round_done=True, notified=True,
                            folder_period_start=CUR.start, period_warning=PERIOD_WARNING,
                            **fields))


async def test_new_folder_appears_one_push_and_the_flag(tmp_path):
    ah = FakeAh(tmp_path, periods=[CUR])
    ctx = make_ctx(tmp_path, ah=ah)
    early(ctx, ah)
    before = ctx.ledger.load(W)
    n_writes = len(ah.writes)
    await tick(ctx, at(FRI, 8, 0))  # still the old folder only: one look, nothing else
    assert ah.calls == ["bonus_periods"] and ctx.hass.notifications() == []
    assert ctx.ledger.load(W).new_folder is None
    ah.periods = [CUR, NXT]  # the folder of the menu week appears
    await tick(ctx, at(FRI, 9, 0))
    assert [n["message"] for n in ctx.hass.notifications()] == [NEW_FOLDER_TEXT]
    log = ctx.ledger.load(W)
    assert log.new_folder == NXT.start and log.new_folder_notified
    assert new_folder_available(log)
    assert weekmenu_posts(ctx)[-1]["attributes"]["new_folder_available"] is True
    # the menu itself is untouched: no new plan, nothing written
    assert len(ah.writes) == n_writes
    assert log.plan == before.plan and log.period_warning == PERIOD_WARNING
    # one push per week, also over a restart; and no more reads once it is known
    ah.calls.clear()
    await whole_day(ctx, FRI)
    await whole_day(ctx, SAT)
    ctx2 = make_ctx(tmp_path, ah=ah)
    main.load_state(ctx2)
    await whole_day(ctx2, SUN)
    assert len(ctx.hass.notifications()) == 1 and ctx2.hass.notifications() == []
    assert "bonus_periods" not in ah.calls


async def test_new_folder_push_retried_when_ha_was_down(tmp_path):
    ah = FakeAh(tmp_path, periods=[CUR])
    ctx = make_ctx(tmp_path, ah=ah)
    early(ctx, ah)
    ah.periods = [CUR, NXT]
    ctx.hass.fail_posts = True
    await tick(ctx, at(FRI, 9, 0))  # must not raise
    assert ctx.ledger.load(W).new_folder_notified is False
    ctx.hass.fail_posts = False
    n = len(ctx.hass.notifications())
    await tick(ctx, at(FRI, 10, 0))
    assert [x["message"] for x in ctx.hass.notifications()[n:]] == [NEW_FOLDER_TEXT]
    assert ctx.ledger.load(W).new_folder_notified


async def test_new_folder_read_failure_is_quiet(tmp_path):
    ah = FakeAh(tmp_path, periods=[CUR])
    ctx = make_ctx(tmp_path, ah=ah)
    early(ctx, ah)
    ah.raise_on["bonus_periods"] = AhApiError("boom", 500)
    n = len(ctx.hass.notifications())
    await tick(ctx, at(FRI, 9, 0))  # must not raise, nothing reported
    assert len(ctx.hass.notifications()) == n and ctx.ledger.load(W).new_folder is None


async def test_menu_made_with_the_right_folder_reads_nothing_hourly(tmp_path):
    ah = FakeAh(tmp_path, periods=[CUR, NXT])
    ctx = make_ctx(tmp_path, ah=ah)
    confirmed(ctx, round_done=True, notified=True)  # folder_period_start: its own folder
    await whole_day(ctx, SAT)
    assert ah.calls == [] and ctx.hass.notifications() == []


async def test_old_log_with_the_warning_also_gets_the_new_folder(tmp_path):
    """A log from before 0.4 C (no folder_period_start) made with the current folder."""
    ah = FakeAh(tmp_path, periods=[CUR, NXT])
    ctx = make_ctx(tmp_path, ah=ah)
    old = WeekLog(W, plan=plan_a().to_dict(), period_warning=PERIOD_WARNING,
                  round_done=True, notified=True).to_dict()
    for k in ("folder_period_start", "new_folder", "new_folder_notified"):
        del old[k]
    (tmp_path / "weeks").mkdir(exist_ok=True)
    (tmp_path / "weeks" / f"{W.isoformat()}.json").write_text(json.dumps(old), encoding="utf-8")
    await tick(ctx, at(FRI, 9, 0))
    assert [x["message"] for x in ctx.hass.notifications()] == [NEW_FOLDER_TEXT]


async def test_bijwerken_makes_a_new_plan_with_the_new_folder(tmp_path, rebuilt):
    calls, _ = rebuilt
    ah = FakeAh(tmp_path, periods=[CUR])
    ctx = make_ctx(tmp_path, ah=ah)
    early(ctx, ah)
    assert ctx.ledger.load(W).savings == 5.0
    ah.periods = [CUR, NXT]
    await tick(ctx, at(FRI, 9, 0))
    out = await run_round(ctx, at(FRI, 10, 0), "bijwerken")
    assert out.status == "done" and out.warning is None
    # the confirmed menu (plan_a) is rebuilt, against the new folder only
    assert [p.basket for p in calls] == [plan_a().basket] and ah.offers_for == [NXT]
    log = ctx.ledger.load(W)
    assert Plan.from_dict(log.plan).basket == plan_b().basket
    assert log.savings == 3.0  # replaced, not 8.0
    assert log.folder_period_start == NXT.start and log.period_warning is None
    assert not new_folder_available(log)
    # cleaned up: 2 and 3 were only for the old menu
    assert ah.held() == {1: 1, 4: 1, 5: 1} and log.added == {1: 1, 4: 1, 5: 1}
    note = ctx.hass.notifications()[-1]["message"]
    assert "2 producten van je lijst gehaald" in note and PERIOD_WARNING not in note
    assert "Over van het vorige menu" not in note
    assert weekmenu_posts(ctx)[-1]["attributes"]["new_folder_available"] is False
    # the hourly look has nothing more to say
    n = len(ctx.hass.notifications())
    ah.calls.clear()
    await whole_day(ctx, SAT)
    assert len(ctx.hass.notifications()) == n and "bonus_periods" not in ah.calls
    assert len(calls) == 1


async def test_bijwerken_without_the_new_folder_changes_nothing(tmp_path, rebuilt):
    calls, _ = rebuilt
    ah = FakeAh(tmp_path, periods=[CUR])
    ctx = make_ctx(tmp_path, ah=ah)
    early(ctx, ah)
    before = ctx.ledger.load(W)
    out = await run_round(ctx, at(date(2026, 10, 29), 11, 0), "bijwerken")
    assert out.status == "skipped"
    # also on Sunday evening it never remakes the menu with the old folder
    out = await run_round(ctx, at(SUN, 19, 0), "bijwerken")
    assert out.status == "skipped" and ah.offers_for == []
    assert calls == [] and ah.writes == []
    assert ctx.ledger.load(W).plan == before.plan
    # the stdin command does the same
    out = await handle_command(ctx, '{"command": "bijwerken"}')
    assert out.status == "skipped" and calls == []


async def test_old_state_file_with_button_notified_loads(tmp_path):
    ctx = make_ctx(tmp_path)
    ctx.state_path.write_text(json.dumps(
        {"checked": "2026-11-01T08:00", "waiting": None, "button_notified": "2026-11-04"}),
        encoding="utf-8")
    main.load_state(ctx)
    assert ctx.checked == datetime(2026, 11, 1, 8, 0)


# --- v0.4 C fix round 1 -------------------------------------------------------------------


async def test_bijwerken_keeps_evenings_persons_leftovers_and_removed_days(tmp_path):
    """Ruling 40: Menu bijwerken takes the week from the menu it replaces, not from the
    helpers: a removed day stays "geen gerecht", rescaled persons stay. v1.1: the same
    recipes too, matched again (the real rebuild_plan)."""
    za = date(2026, 11, 7)
    first = dish(WO, 101, [1, 2], persons=4)
    first.leftover_date = DO  # woensdag cooks for donderdag too
    ah = FakeAh(tmp_path, periods=[CUR])
    ah.search_results["ui"] = [main.Product(10, "AH Uien", None, 1.0, 1.0, False, None,
                                            True, None)]
    ctx = make_ctx(tmp_path, ah=ah)
    early(ctx, ah, plan=make_plan([first, dish(VR, 102, [2, 3]), dish(za, 103, [6])]))
    # in the app: zaterdag "Gerecht weghalen", vrijdag set to 3 persons
    wlog = ctx.ledger.load(W)
    old = Plan.from_dict(wlog.plan)
    await apply(ah, ctx.ledger, wlog, main.remove_dish(old, za), replaced=old,
                today=date(2026, 10, 29))
    wlog = ctx.ledger.load(W)
    for x in wlog.plan["dishes"]:
        if x["date"] == VR.isoformat():
            x["persons"] = 3
    ctx.ledger.save(wlog)
    assert 6 not in ah.held()
    # helpers say something else: they must not count for bijwerken
    for eid in helper_persons().values():
        ctx.hass.states[eid] = "1"
    ah.periods = [CUR, NXT]
    await tick(ctx, at(FRI, 9, 0))
    out = await run_round(ctx, at(FRI, 10, 0), "bijwerken")
    assert out.status == "done"
    log = ctx.ledger.load(W)
    new = Plan.from_dict(log.plan)
    assert {d.date: d.recipe.id for d in new.dishes} == {WO: 101, VR: 102}
    assert new.removed_days == [za] and za not in {d.date for d in new.dishes}
    assert {d.date: d.persons for d in new.dishes} == {WO: 4, VR: 3}
    assert {d.date: d.leftover_date for d in new.dishes} == {WO: DO, VR: None}
    assert set(ah.held()) == {10}  # nothing for zaterdag, the old products off
    view = weekmenu_posts(ctx)[-1]["attributes"]
    assert view["geen_gerecht"] == [za.isoformat()]
    assert view["new_folder_available"] is False


async def test_monday_clears_a_stale_new_folder_flag_once(tmp_path):
    ah = FakeAh(tmp_path, periods=[CUR])
    ctx = make_ctx(tmp_path, ah=ah)
    early(ctx, ah)
    ah.periods = [CUR, NXT]
    await tick(ctx, at(FRI, 9, 0))
    assert weekmenu_posts(ctx)[-1]["attributes"]["new_folder_available"] is True
    n_notes, n_posts = len(ctx.hass.notifications()), len(weekmenu_posts(ctx))
    await tick(ctx, at(MON, 8, 0))  # the target is the week of 11 November now
    posts = weekmenu_posts(ctx)[n_posts:]
    assert len(posts) == 1 and posts[0]["attributes"]["week"] == W.isoformat()
    assert posts[0]["attributes"]["new_folder_available"] is False
    assert not new_folder_available(ctx.ledger.load(W))
    assert ctx.ledger.load(W).plan is not None  # the menu itself stays
    await whole_day(ctx, MON)
    await whole_day(ctx, date(2026, 11, 3))
    assert len(weekmenu_posts(ctx)) == n_posts + 1
    assert len(ctx.hass.notifications()) == n_notes


async def test_hourly_look_is_no_round_for_the_app(tmp_path):
    """The new-folder look takes the lock but is no menu round: in_round stays off and
    the round count does not move."""
    ah = FakeAh(tmp_path, periods=[CUR])
    ctx = make_ctx(tmp_path, ah=ah)
    early(ctx, ah)
    rounds = ctx.rounds
    seen = []

    async def watching_notify(title, message, url=None):
        seen.append((ctx.lock.locked(), ctx.in_round))

    ctx.hass.notify = watching_notify
    ah.periods = [CUR, NXT]
    await tick(ctx, at(FRI, 9, 0))
    assert seen == [(True, False)] and ctx.rounds == rounds


# --- Ruling 42: never into an order of another week ---------------------------------

W2 = date(2026, 11, 11)  # the menu week on Monday 2 November


async def test_monday_confirm_with_this_weeks_open_order_writes_nothing(tmp_path):
    """Monday 08:00, the week of the 11th confirmed: the open order is for Wednesday 4
    November. The plan is saved, nothing goes into that order, and the hourly look
    writes it once that order is gone (the list)."""
    from app.ahclient import ActiveOrder
    ah = FakeAh(tmp_path, periods=[CUR, NXT], order=ActiveOrder(5, {9: 1}, WED))
    ctx = make_ctx(tmp_path, ah=ah)
    result = await confirm(ctx, at(MON, 8, 0), make_plan([dish(W2, 101, [1, 2], 2.0),
                                                         dish(W2 + timedelta(days=1), 102,
                                                              [2, 3], 3.0)]))
    assert result.held
    assert ah.writes == [] and ah.order.items == {9: 1}
    log = ctx.ledger.load(W2)
    assert log.plan is not None and log.held and log.added == {}
    assert log.round_done is False  # the schedule completes it later

    # the hourly looks on Monday and Tuesday: the order is still open, nothing happens
    history_reads = len(ah.history_for)
    await whole_day(ctx, MON)
    await whole_day(ctx, date(2026, 11, 3))
    assert ah.writes == [] and ctx.hass.notifications() == []
    assert len(ah.history_for) == history_reads  # no hourly purchase-history reads
    assert ctx.ledger.load(W2).round_done is False

    # Wednesday: delivered, no active order any more, so the list is usable
    ah.order = None
    await tick(ctx, at(WED, 12, 0))
    assert ah.listed == {1: 1, 2: 2, 3: 1}  # the same menu
    log = ctx.ledger.load(W2)
    assert log.round_done is True and log.held is None and log.added_to == "list"
    (note,) = ctx.hass.notifications()
    assert "Alles staat op Mijn lijst" in note["message"]
    n = len(ah.writes)
    await whole_day(ctx, date(2026, 11, 5))
    assert len(ah.writes) == n and len(ctx.hass.notifications()) == 1


async def test_held_menu_goes_into_the_order_for_its_own_week(tmp_path):
    from app.ahclient import ActiveOrder
    ah = FakeAh(tmp_path, periods=[CUR, NXT], order=ActiveOrder(5, {}, WED))
    ctx = make_ctx(tmp_path, ah=ah)
    await confirm(ctx, at(MON, 8, 0), make_plan([dish(W2, 101, [1, 2], 2.0),
                                                dish(W2 + timedelta(days=1), 102, [2, 3])]))
    ah.order = ActiveOrder(6, {}, date(2026, 11, 13))  # the order for the menu week
    await tick(ctx, at(date(2026, 11, 7), 10, 0))
    assert ah.order.items == {1: 1, 2: 2, 3: 1} and ah.listed == {}
    assert ctx.ledger.load(W2).added_to == "order:6"


async def test_unknown_delivery_day_writes_nothing(tmp_path):
    from app.ahclient import ActiveOrder
    ah = FakeAh(tmp_path, periods=[CUR, NXT], order=ActiveOrder(5, {}, None))
    ctx = make_ctx(tmp_path, ah=ah)
    confirmed(ctx)
    out = await complete_round(ctx, at(SUN, 10, 0))  # the menu week of the 4th
    assert out.status == "done" and out.result.held and ah.writes == []
    assert ctx.ledger.load(W).round_done is False
    assert ctx.ledger.load(W).completion_tried is False  # tried again the next hour


async def test_held_week_the_schedule_moved_past_gets_one_push_to_add_by_hand(tmp_path):
    """Ruling 44: on Monday 9 November the schedule works on the 18th; the held menu
    of the 11th is never retried, so one push names what to add in the AH app."""
    from app.ahclient import ActiveOrder
    ah = FakeAh(tmp_path, periods=[CUR, NXT], order=ActiveOrder(5, {}, date(2026, 10, 28)))
    ctx = make_ctx(tmp_path, ah=ah)
    await confirm(ctx, at(MON, 8, 0), make_plan([dish(W2, 101, [1, 2], 2.0),
                                                dish(W2 + timedelta(days=1), 102, [2, 3])]))
    manual = ("Deze producten konden niet in je bestelling. Voeg ze zelf toe in de "
              "AH-app: Product 1, Product 2, Product 3")
    await whole_day(ctx, date(2026, 11, 9))
    await whole_day(ctx, date(2026, 11, 10))
    pushes = [n["message"] for n in ctx.hass.notifications() if n["message"] == manual]
    assert len(pushes) == 1 and ah.writes == []
    assert ctx.ledger.load(W2).held_notified is True
    ctx2 = make_ctx(tmp_path, ah=ah)  # restart: the marker is on disk
    main.load_state(ctx2)
    await whole_day(ctx2, date(2026, 11, 10))
    assert [n for n in ctx2.hass.notifications() if n["message"] == manual] == []


async def test_held_push_retried_when_ha_was_down(tmp_path):
    from app.ahclient import ActiveOrder
    ah = FakeAh(tmp_path, periods=[CUR, NXT], order=ActiveOrder(5, {}, date(2026, 10, 28)))
    ctx = make_ctx(tmp_path, ah=ah)
    await confirm(ctx, at(MON, 8, 0), make_plan([dish(W2, 101, [1, 2], 2.0)]))
    ctx.hass.fail_posts = True
    await main.notify_stale_held(ctx, date(2026, 11, 18))
    assert ctx.ledger.load(W2).held_notified is False
    ctx.hass.fail_posts = False
    await main.notify_stale_held(ctx, date(2026, 11, 18))
    assert ctx.ledger.load(W2).held_notified is True


# --- v0.6: files from a newer add-on --------------------------------------------------


def _too_new_week(tmp_path, **extra):
    data = {**WeekLog(W, plan=plan_a().to_dict(), **extra).to_dict(), "schema": 99}
    path = tmp_path / "weeks" / f"{W.isoformat()}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _update_notes(ctx):
    from app.durable import UPDATE_MESSAGE
    return [n for n in ctx.hass.notifications() if n["message"] == UPDATE_MESSAGE]


@pytest.mark.parametrize("complete", [False, True])
async def test_too_new_week_log_writes_nothing_and_says_so_once(tmp_path, complete):
    from app.durable import UPDATE_MESSAGE
    path = _too_new_week(tmp_path)
    before = path.read_bytes()
    ah = FakeAh(tmp_path)
    ctx = make_ctx(tmp_path, ah=ah)

    async def go():
        return await (complete_round(ctx, SUNDAY) if complete
                      else run_round(ctx, SUNDAY, "bijwerken"))

    out = await go()
    again = await go()
    assert out.status == again.status == "error" and out.error == UPDATE_MESSAGE
    assert ctx.error == UPDATE_MESSAGE
    assert ah.writes == []
    assert path.read_bytes() == before
    assert not list(path.parent.glob("*.bak")) and not list(path.parent.glob("*.tmp"))
    assert len(_update_notes(ctx)) == 1
    assert ctx.ledger.load(W).plan == plan_a().to_dict()  # still readable for display


async def test_too_new_tokens_stop_the_round_before_any_ah_call(tmp_path):
    ah = FakeAh(tmp_path)
    tokens = tmp_path / "tokens.json"
    tokens.write_text(json.dumps({"schema": 7, "access_token": "a", "refresh_token": "r",
                                  "expires_at": 9e9}), encoding="utf-8")
    before = tokens.read_bytes()
    ctx = make_ctx(tmp_path, ah=ah)
    out = await run_round(ctx, SUNDAY, "bijwerken")
    assert out.status == "error" and ah.writes == [] and ah.calls == []
    assert tokens.read_bytes() == before
    assert len(_update_notes(ctx)) == 1


async def test_too_new_state_and_total_are_left_alone_and_said_once(tmp_path):
    ctx = make_ctx(tmp_path)
    ctx.state_path.write_text('{"schema": 3, "checked": "2026-11-01T07:00"}',
                              encoding="utf-8")
    ctx.total_path.write_text('{"schema": 3, "total": 50.0}', encoding="utf-8")
    state, total = ctx.state_path.read_bytes(), ctx.total_path.read_bytes()
    main.load_state(ctx)  # reading is fine
    assert ctx.checked == datetime(2026, 11, 1, 7, 0)
    confirmed(ctx)
    out = await complete_round(ctx, SUNDAY)
    assert out.status == "done"  # only a week log or the tokens stop a round
    await tick(ctx, SUNDAY.replace(hour=10))
    await tick(ctx, SUNDAY.replace(hour=11))
    assert ctx.state_path.read_bytes() == state
    assert ctx.total_path.read_bytes() == total
    assert ctx.too_new == {"state.json", "totaal.json"}
    assert len(_update_notes(ctx)) == 1


async def test_too_new_weeks_are_not_counted_nor_pushed(tmp_path):
    path = _too_new_week(tmp_path, savings=4.0, held="x")
    before = path.read_bytes()
    ctx = make_ctx(tmp_path)
    await update_total(ctx, date(2026, 11, 12))
    assert json.loads(ctx.total_path.read_text(encoding="utf-8"))["total"] == 0.0
    await main.notify_stale_held(ctx, date(2026, 11, 11))
    await main.check_new_folder(ctx, W)
    await main.close_stale_new_folder(ctx, date(2026, 11, 11))
    assert ctx.hass.notifications() == []
    assert path.read_bytes() == before


def test_state_migration_v0_to_v1_and_old_state_loads(tmp_path):
    ctx = make_ctx(tmp_path)
    old = {"checked": "2026-11-01T08:00", "waiting": {"week": "2026-11-04",
                                                       "expected": None}}
    assert main.migrate_state_v0_to_v1(old) == {**old, "schema": 1}
    ctx.state_path.write_text(json.dumps(old), encoding="utf-8")
    main.load_state(ctx)
    assert ctx.checked == datetime(2026, 11, 1, 8, 0) and ctx.waiting.week == W
    main._save_state(ctx)
    assert json.loads(ctx.state_path.read_text(encoding="utf-8"))["schema"] == 1
    assert json.loads((tmp_path / "state.json.bak").read_text(encoding="utf-8")) == old


async def test_startup_scan_notes_newer_files_and_the_first_look_says_so(tmp_path):
    ctx = make_ctx(tmp_path)
    (tmp_path / "niet_thuis.json").write_text('{"schema": 2}', encoding="utf-8")
    (tmp_path / "state.json").write_text('{"schema": 1}', encoding="utf-8")  # current
    main.scan_too_new(ctx)
    assert ctx.too_new == {"niet_thuis.json"}
    await main.notify_too_new(ctx)
    await main.notify_too_new(ctx)
    assert len(_update_notes(ctx)) == 1


# --- v0.6: the planner and the log saves off the event loop ---------------------------


async def test_round_saves_the_log_in_a_worker_thread(tmp_path, monkeypatch):
    import threading

    ctx = make_ctx(tmp_path)
    confirmed(ctx)
    threads = []
    real = ctx.ledger.save

    def spy(wlog):
        threads.append(threading.current_thread() is threading.main_thread())
        real(wlog)

    monkeypatch.setattr(ctx.ledger, "save", spy)
    out = await complete_round(ctx, SUNDAY)
    assert out.status == "done"
    # writer saves (write-ahead, inside the AH-call callbacks) stay on the loop thread;
    # the round's own saves (round_done, notified, staples, counted) go to a thread
    assert False in threads and True in threads
    assert ctx.ledger.load(W).round_done and ctx.ledger.load(W).notified
