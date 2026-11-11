"""1.0.0 final review fixes: I1 (writes left over from a pause), I2 (honest repick
text), I3 (the recount retries once a day), M6 (notify_tonight logs at most hourly)."""
from datetime import date, datetime

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
