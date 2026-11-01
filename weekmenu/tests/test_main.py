import json
from datetime import date, datetime

import pytest

from app import main
from app.ahclient import AhLoginExpired, AhNotJson
from app.ledger import Ledger, WeekLog
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


async def test_schema_retries_hourly_until_18_when_next_period_missing(tmp_path, planned):
    calls, _ = planned
    ah = FakeAh(tmp_path, periods=[CURRENT])
    ctx = make_ctx(tmp_path, ah=ah)

    out = await run_round(ctx, SUNDAY, "schema")
    assert out.status == "retry" and out.retry_at == datetime(2026, 11, 1, 9, 0)
    out = await run_round(ctx, datetime(2026, 11, 1, 17, 0), "schema")
    assert out.retry_at == datetime(2026, 11, 1, 18, 0)
    assert calls == [] and ah.writes == [] and ctx.hass.notifications() == []

    # the schedule loop: Sunday 08:00 round, nothing at 08:30, the retry at 09:00
    ctx.retry_at = None
    await tick(ctx, SUNDAY)
    assert ctx.retry_at == datetime(2026, 11, 1, 9, 0)
    await tick(ctx, datetime(2026, 11, 1, 8, 30))
    assert ah.calls.count("bonus_periods") == 3
    ah.periods = [CURRENT, NEXT]  # the folder appears
    await tick(ctx, datetime(2026, 11, 1, 9, 0))
    assert ctx.retry_at is None
    assert ah.offers_for == [NEXT] and len(calls) == 1
    assert ah.held() == {1: 1, 2: 2, 3: 1}
    # no second Sunday round for the same day
    await tick(ctx, datetime(2026, 11, 1, 10, 0))
    assert len(calls) == 1


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
    assert "bonus_periods" not in ctx.client.calls
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
    assert len(ah.writes) == 2  # stopped, nothing retried

    # "restart": a fresh context on the same /data, same AH state
    ah.fail_on_call = None
    ctx2 = make_ctx(tmp_path, ah=ah)
    ctx2.ledger = Ledger(tmp_path / "weeks")
    out = await run_round(ctx2, datetime(2026, 11, 1, 8, 5), "schema")
    assert out.status == "done"
    assert len(calls) == 1  # the plan of the first round is reused
    assert ah.held() == plan_a().basket == {1: 1, 2: 2, 3: 1}
    # and once more: nothing at all
    n = len(ah.writes)
    await run_round(ctx2, datetime(2026, 11, 1, 8, 10), "nu_maken")
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
    hass = FakeHass(tmp_path / "totaal.json", aantal=3, ouders="donderdag")
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


async def test_schedule_done_survives_restart(tmp_path, planned):
    calls, _ = planned
    ctx = make_ctx(tmp_path)
    await tick(ctx, SUNDAY)
    assert len(calls) == 1
    ctx2 = make_ctx(tmp_path)
    main.load_state(ctx2)
    await tick(ctx2, datetime(2026, 11, 1, 9, 0))
    assert len(calls) == 1 and ctx2.hass.notifications() == []
