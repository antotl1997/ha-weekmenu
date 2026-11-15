"""1.0.0 final review fixes: I1 (writes left over from a pause), I2 (honest repick
text), I3 (the recount retries once a day), M6 (notify_tonight logs at most hourly)."""
from datetime import date, datetime, timedelta

import pytest

from app import main
from app.health import PAUSED_NOTE
from app.ledger import WeekLog
from app.web import check_view, make_app
from app.writer import apply, manual_note
from tests.fakes import W, dish, make_ctx, make_plan
from tests.test_v09_web import WO, p, planned


def pause(ctx):
    ctx.health.failed("lijst", "vorm")
    ctx.health.failed("lijst", "vorm")
    assert ctx.health.writes_paused


# --- I1 -------------------------------------------------------------------------------


async def test_paused_apply_in_the_target_week_marks_the_log_held(tmp_path):
    ctx = make_ctx(tmp_path)
    pause(ctx)
    res = await apply(ctx.client, ctx.ledger, WeekLog(W),
                      make_plan([dish(W, 101, [1, 2], 2.0)]), today=date(2026, 11, 1))
    assert res.held == PAUSED_NOTE
    saved = ctx.ledger.load(W)
    assert saved.held == PAUSED_NOTE and not saved.round_done


async def test_paused_apply_in_a_week_no_round_retries_names_what_to_add(tmp_path):
    ctx = make_ctx(tmp_path)
    pause(ctx)
    plan = make_plan([dish(W, 101, [1, 2], 2.0)])
    res = await apply(ctx.client, ctx.ledger, WeekLog(W), plan, today=date(2026, 11, 6))
    assert res.held == manual_note([plan.products[1].title, plan.products[2].title])
    assert ctx.ledger.load(W).held == PAUSED_NOTE and ctx.client.writes == []


async def test_paused_apply_with_nothing_to_add_leaves_held_alone(tmp_path):
    ctx = make_ctx(tmp_path)
    wlog = WeekLog(W)
    wlog.added = {1: 1, 2: 1}
    pause(ctx)
    res = await apply(ctx.client, ctx.ledger, wlog, make_plan([dish(W, 101, [1, 2], 2.0)]),
                      today=date(2026, 11, 6))
    assert res.held is None and ctx.ledger.load(W).held is None


def test_check_card_counts_what_was_never_added_without_a_held_note():
    plan = make_plan([dish(W, 101, [1, 2], 2.0)])
    wlog = WeekLog(W, plan=plan.to_dict())
    wlog.added = {1: 1}
    check = check_view(wlog, plan, None)
    assert check["ok"] is False and check["counts"]["missing"] == 1
    assert check["missing"] == [{"product": plan.products[2].title, "qty": 1}]
    wlog.added = {1: 1, 2: 1}
    assert check_view(wlog, plan, None)["ok"] is True


async def test_pause_swap_in_the_running_week_resume_the_card_is_not_ok(
        aiohttp_client, tmp_path):
    """The reviewer's case: a change in the running week while paused, then resume."""
    ctx = await planned(tmp_path, now=datetime(2026, 11, 6, 9, 0))
    ctx.client.search_results["kip"] = [p(9, "AH Kippendijen", "500 g")]
    client = await aiohttp_client(make_app(ctx))
    assert (await client.get("api/products", params={"q": "kip"})).status == 200
    before = list(ctx.client.writes)
    pause(ctx)
    r = await client.post(f"api/dish/{WO}/correct",
                          json={"ingredient": "300 g kipfilet", "kind": "product",
                                "product_id": 9})
    assert r.status == 200, await r.text()
    body = await r.json()
    assert body["held"] == manual_note(["AH Kippendijen"])  # nothing will retry it
    assert ctx.client.writes == before
    ctx.health.resume("test")
    week = await (await client.get(f"api/week?week={W.isoformat()}")).json()
    assert week["writes_paused"] is None
    assert week["check"]["ok"] is False and week["check"]["counts"]["held"] == 1
    assert "AH Kippendijen" in week["check"]["held"]
    # Ruling 44: the next hourly look pushes once what to add by hand
    await main.notify_stale_held(ctx, date(2026, 11, 11))
    msgs = [n["message"] for n in ctx.hass.notifications()]
    assert manual_note(["AH Kippendijen"]) in msgs
    assert ctx.ledger.load(W).held_notified


# --- I3: the recount looks again once a day until due_day + 7 ---------------------------

from app import reconcile  # noqa: E402
from app.ahclient import AhApiError, AhLoginExpired  # noqa: E402
from app.reconcile import (  # noqa: E402
    BUSY,
    CANCELLED,
    NO_ORDER,
    NOT_DELIVERED,
    OK,
    UNCHECKABLE,
    UNREADABLE,
    WAIT,
    DeliveredOrder,
)
from tests.test_staples import history_routes, make_client, no_delay  # noqa: E402,F401
from tests.test_v10_reconcile import ctx_with_week  # noqa: E402

DUE = date(2026, 11, 7)  # delivery on Friday 6 November: recounted from Saturday


def at(day: date, hour: int = 9) -> datetime:
    return datetime(day.year, day.month, day.day, hour, 0)


def entry(ctx):
    return ctx.reconciliations.load()[W.isoformat()]


async def test_ahclient_tells_not_yet_delivered_from_cancelled(respx_mock, tmp_path, no_delay):
    respx_mock.get(url__regex=r".*/order/v1/7/details-grouped-by-taxonomy").respond(
        json={"orderState": "SUBMITTED", "deliveryDate": "2026-11-06"})
    history_routes(respx_mock)
    client = make_client(tmp_path)
    got = await client.delivered_order(7)
    assert got is not None and not got.delivered and got.state == "SUBMITTED"
    assert got.items == {} and client.health.fails["bestelling"] == 0
    assert await client.delivered_order(900004) is None  # cancelled: final
    assert (await client.delivered_order(900002)).delivered


async def test_not_yet_delivered_waits_a_day_and_then_counts(tmp_path):
    ctx = ctx_with_week(tmp_path, at(DUE), [DeliveredOrder(5, None, {}, state="SUBMITTED")],
                        added_to="order:5", delivery=date(2026, 11, 6))
    await reconcile.reconcile_due(ctx, at(DUE))
    got = entry(ctx)
    assert got["status"] == WAIT and got["tries"] == 1 and got["reason"] == NOT_DELIVERED
    assert got["next"] == (DUE + timedelta(days=1)).isoformat()
    for hour in (10, 15, 22):  # not again the same day
        await reconcile.reconcile_due(ctx, at(DUE, hour))
    assert ctx.client.reads == [("order", 5)]
    ctx.client.delivered[5] = DeliveredOrder(5, date(2026, 11, 7), {1: 2, 2: 1})
    await reconcile.reconcile_due(ctx, at(DUE + timedelta(days=1)))
    got = entry(ctx)
    assert got["status"] == OK and got["bought_share"] == {"bought": 2, "added": 5}
    assert len(ctx.client.reads) == 2
    await reconcile.reconcile_due(ctx, at(DUE + timedelta(days=2)))
    assert len(ctx.client.reads) == 2  # final


async def test_a_cancelled_order_is_final_at_once(tmp_path):
    ctx = ctx_with_week(tmp_path, at(DUE), [], added_to="order:5",
                        delivery=date(2026, 11, 6))
    await reconcile.reconcile_due(ctx, at(DUE))
    got = entry(ctx)
    assert got["status"] == UNCHECKABLE and got["reason"] == CANCELLED
    await reconcile.reconcile_due(ctx, at(DUE + timedelta(days=1)))
    assert len(ctx.client.reads) == 1


@pytest.mark.parametrize("error", [AhApiError("boom", 503), AhApiError("timeout"),
                                   AhLoginExpired("weg", 401)])
async def test_a_transient_error_is_retried_once_a_day(tmp_path, error):
    ctx = ctx_with_week(tmp_path, at(DUE), [], added_to="list", delivery=date(2026, 11, 6))
    ctx.client.raise_on["delivered_orders"] = error
    await reconcile.reconcile_due(ctx, at(DUE))
    got = entry(ctx)
    assert got["status"] == WAIT and got["reason"] == UNREADABLE and got["tries"] == 1
    ctx.login_expired = False  # logged in again
    del ctx.client.raise_on["delivered_orders"]
    ctx.client.delivered[8] = DeliveredOrder(8, date(2026, 11, 6), {1: 2})
    await reconcile.reconcile_due(ctx, at(DUE, 23))
    assert ctx.client.calls.count("delivered_orders") == 1  # once a day
    await reconcile.reconcile_due(ctx, at(DUE + timedelta(days=1)))
    assert entry(ctx)["status"] == OK


async def test_no_order_is_retried_until_seven_days_after_due_then_final(tmp_path):
    ctx = ctx_with_week(tmp_path, at(DUE), [], added_to="list", delivery=date(2026, 11, 6))
    for i in range(7):
        await reconcile.reconcile_due(ctx, at(DUE + timedelta(days=i)))
        got = entry(ctx)
        assert got["status"] == WAIT and got["tries"] == i + 1 and got["reason"] == NO_ORDER
    await reconcile.reconcile_due(ctx, at(DUE + timedelta(days=7)))
    got = entry(ctx)
    assert got["status"] == UNCHECKABLE and got["reason"] == NO_ORDER and got["tries"] == 8
    for i in (8, 9, 20):
        await reconcile.reconcile_due(ctx, at(DUE + timedelta(days=i)))
    assert ctx.client.calls.count("delivered_orders") == 8


async def test_a_busy_marker_left_by_a_crash_counts_as_a_failed_try(tmp_path):
    """Review M1: a crash between the marker and the result is retried the next day."""
    ctx = ctx_with_week(tmp_path, at(DUE), [DeliveredOrder(8, date(2026, 11, 6), {1: 2})],
                        added_to="list", delivery=date(2026, 11, 6))
    ctx.reconciliations.put(W.isoformat(), {"status": BUSY, "tries": 1,
                                            "at": at(DUE).isoformat(timespec="minutes")})
    await reconcile.reconcile_due(ctx, at(DUE, 15))
    assert ctx.client.calls.count("delivered_orders") == 0  # same day: not yet
    await reconcile.reconcile_due(ctx, at(DUE + timedelta(days=1)))
    assert entry(ctx)["status"] == OK


async def test_waiting_weeks_stay_hidden_in_the_savings_api(aiohttp_client, tmp_path):
    ctx = ctx_with_week(tmp_path, at(DUE), [], added_to="list", delivery=date(2026, 11, 6))
    await reconcile.reconcile_due(ctx, at(DUE))
    client = await aiohttp_client(make_app(ctx))
    weeks = (await (await client.get("api/savings")).json())["weeks"]
    assert next(w for w in weeks if w["week"] == W.isoformat())["reconciled"] is None


# --- M6: notify_tonight warns at most once an hour when HA cannot be read ---------------

from tests.test_v08 import tonight_ctx, tonights  # noqa: E402


async def test_tonight_warns_at_most_once_an_hour_and_still_sends_later(tmp_path, caplog):
    ctx = tonight_ctx(tmp_path, datetime(2026, 11, 4, 16, 0))
    real = ctx.hass.read_settings

    async def broken():
        raise OSError("ha weg")
    ctx.hass.read_settings = broken
    caplog.set_level("WARNING", logger="weekmenu")
    for minute in range(0, 90):  # 16:00 to 17:29, every minute
        await main.notify_tonight(ctx, datetime(2026, 11, 4, 16 + minute // 60, minute % 60))
    warned = [r for r in caplog.records if "Vanavond" in r.getMessage()]
    assert len(warned) == 2  # 16:00 and 17:00
    ctx.hass.read_settings = real  # HA back: the next minute still sends
    await main.notify_tonight(ctx, datetime(2026, 11, 4, 17, 30))
    assert any("Vanavond" in m for _, m in tonights(ctx))


# --- M5: Terugdraaien in Mijn correcties does not wait a whole menu round ----------------


async def test_remove_override_does_not_wait_for_a_menu_round(aiohttp_client, tmp_path):
    from app.hass import BUSY
    ctx = await planned(tmp_path)
    client = await aiohttp_client(make_app(ctx))
    await client.post(f"api/dish/{WO}/correct",
                      json={"ingredient": "300 g kipfilet", "kind": "pantry"})
    ctx.in_round = True
    r = await client.post("api/overrides/remove", json={"key": "kipfilet"})
    assert r.status == 409 and (await r.json())["error"] == BUSY
    assert "kipfilet" in ctx.overrides.load()


# --- M7: no order id in the log or the diagnose ring ---------------------------------


def test_short_error_hides_numeric_path_segments():
    e = AhApiError("GET /mobile-services/order/v1/123456789/details-grouped-by-taxonomy: "
                   "HTTP 500", 500)
    assert main.short_error(e) == ("GET /mobile-services/order/v1/<id>/"
                                   "details-grouped-by-taxonomy: HTTP 500")
    assert main.short_error(AhApiError("GET /x/v2/items failed: ReadTimeout")) == \
        "GET /x/v2/items failed: ReadTimeout"


# --- Ruling 53: recount reads never feed ApiHealth; a changed shape is final ------------

from app.ahclient import AhShapeError  # noqa: E402
from app.health import PAUSED_MESSAGE  # noqa: E402


async def test_a_changed_order_shape_in_the_recount_does_not_pause_writing(
        respx_mock, tmp_path, no_delay):
    respx_mock.get(url__regex=r".*/order/v1/\d+/details-grouped-by-taxonomy").respond(
        json={"orderState": "DELIVERED", "groupedProductsInTaxonomy": "geen lijst"})
    client = make_client(tmp_path)
    for _ in range(3):
        with pytest.raises(AhShapeError):
            await client.delivered_order(1)
    assert not client.health.writes_paused and client.health.fails["bestelling"] == 0


async def test_a_shape_error_makes_the_week_final_at_once(tmp_path):
    ctx = ctx_with_week(tmp_path, at(DUE), [], added_to="list", delivery=date(2026, 11, 6))
    ctx.client.raise_on["delivered_orders"] = AhShapeError("bestelling: vorm", 200)
    await reconcile.reconcile_due(ctx, at(DUE))
    got = entry(ctx)
    assert got["status"] == UNCHECKABLE and got["tries"] == 1
    assert not ctx.health.writes_paused
    assert PAUSED_MESSAGE not in [n["message"] for n in ctx.hass.notifications()]
    del ctx.client.raise_on["delivered_orders"]
    await reconcile.reconcile_due(ctx, at(DUE + timedelta(days=1)))
    assert ctx.client.calls.count("delivered_orders") == 1  # never again


# --- Ruling 53: Opnieuw proberen clears the stale pause note of the target week ---------


async def test_retry_by_hand_clears_the_pause_note_of_the_target_week(
        aiohttp_client, tmp_path):
    ctx = make_ctx(tmp_path)  # SUNDAY: the target week is W
    pause(ctx)
    await apply(ctx.client, ctx.ledger, WeekLog(W), make_plan([dish(W, 101, [1, 2], 2.0)]),
                today=date(2026, 11, 1))
    assert ctx.ledger.load(W).held == PAUSED_NOTE
    client = await aiohttp_client(make_app(ctx))
    r = await client.post("api/diagnose/retry")
    assert (await r.json()) == {"writes_paused": False}
    saved = ctx.ledger.load(W)
    assert saved.held is None and not saved.round_done  # the hourly look still adds them
    week = await (await client.get(f"api/week?week={W.isoformat()}")).json()
    assert week["held"] is None
    assert week["check"]["ok"] is False and week["check"]["counts"]["missing"] == 2
