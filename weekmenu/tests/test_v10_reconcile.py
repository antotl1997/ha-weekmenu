"""v1.0 points 3 and 4: "Besparing nagerekend" after the delivery (read-only, at most once
per menu week) and the two numbers in the app and the diagnose."""

import json
from datetime import date, datetime, time, timedelta

import pytest

from app import main, reconcile
from app.ahclient import ActiveOrder, AhApiError, AhLoginExpired, Product
from app.diag import diagnose
from app.ledger import WeekLog
from app.overrides import make_entry
from app.reconcile import (
    OK,
    RECON_SCHEMA,
    UNCHECKABLE,
    DeliveredOrder,
    Reconciliations,
    bought_share,
    compute,
    due_day,
    untouched_share,
)
from app.staples import Staple
from app.tokens import Tokens
from app.web import make_app
from app.writer import OFFER, apply, apply_extras
from tests.fakes import FakeAh, W, dish, make_ctx, make_plan
from tests.test_staples import history_routes, make_client, no_delay  # noqa: F401


def p(pid, regular=4.0, now=3.0, mech="2e halve prijs"):
    return Product(pid, f"Product {pid}", None, regular, now, mech is not None, mech, True, None)


# --- the writer remembers the delivery day and each offer's saving ------------------------


async def test_writer_keeps_the_delivery_day_of_the_order_it_wrote_to(tmp_path):
    ah = FakeAh(tmp_path, order=ActiveOrder(5, {}, date(2026, 11, 6)))
    ctx = make_ctx(tmp_path, ah=ah)
    log = ctx.ledger.load(W)
    await apply(ah, ctx.ledger, log, make_plan([dish(W, 1, [1])]))
    assert ctx.ledger.load(W).delivery == date(2026, 11, 6)
    # an order for another week gets nothing, and its day is not this week's delivery
    ah2 = FakeAh(tmp_path / "b", order=ActiveOrder(6, {}, date(2026, 11, 20)))
    (tmp_path / "b").mkdir()
    ctx2 = make_ctx(tmp_path / "b", ah=ah2)
    await apply(ah2, ctx2.ledger, ctx2.ledger.load(W), make_plan([dish(W, 1, [1])]))
    assert ctx2.ledger.load(W).delivery is None


async def test_writer_keeps_each_offers_saving(tmp_path):
    ctx = make_ctx(tmp_path)
    log = ctx.ledger.load(W)
    await apply_extras(ctx.client, ctx.ledger, log, {40: 2}, {40: "Chips"}, kind=OFFER,
                       savings={40: 1.25})
    log = ctx.ledger.load(W)
    assert log.offers_added == {40: 2} and log.offers_saved == {40: 1.25}
    assert log.offers_savings == 1.25


def test_old_week_logs_load_without_the_new_fields(tmp_path):
    ctx = make_ctx(tmp_path)
    (tmp_path / "weeks").mkdir()
    (tmp_path / "weeks" / f"{W}.json").write_text(json.dumps({"week": W.isoformat()}),
                                                  encoding="utf-8")
    log = ctx.ledger.load(W)
    assert log.delivery is None and log.offers_saved == {}


# --- AH: the delivered order, read only ------------------------------------------------------


async def test_delivered_orders_in_a_week(respx_mock, tmp_path, no_delay):
    seen, details = history_routes(respx_mock)
    client = make_client(tmp_path)
    got = await client.delivered_orders(date(2026, 10, 28), date(2026, 11, 3))
    assert [(o.day, len(o.items)) for o in got] == [(date(2026, 10, 28), 9)]
    assert got[0].items[548041] == 1
    assert [c.request.url.path for c in details.calls] == [
        "/mobile-services/order/v1/900001/details-grouped-by-taxonomy"]
    assert all(q.lstrip().startswith("query") for q in seen)  # never a mutation
    assert client.health.fails["bestelling"] == 0


async def test_delivered_order_by_id_and_a_cancelled_one(respx_mock, tmp_path, no_delay):
    history_routes(respx_mock)
    client = make_client(tmp_path)
    got = await client.delivered_order(900002)
    assert got.day == date(2026, 10, 7) and len(got.items) == 12
    assert await client.delivered_order(900004) is None  # cancelled


async def test_a_changed_order_shape_counts_for_api_health(respx_mock, tmp_path, no_delay):
    respx_mock.get(url__regex=r".*/order/v1/\d+/details-grouped-by-taxonomy").respond(
        json={"orderState": "DELIVERED", "groupedProductsInTaxonomy": "geen lijst"})
    client = make_client(tmp_path)
    for _ in range(2):
        with pytest.raises(AhApiError):
            await client.delivered_order(1)
    assert client.health.writes_paused and client.health.paused_by == "bestelling"


# --- the sums ---------------------------------------------------------------------------------


def week_log(**kw) -> WeekLog:
    plan = make_plan([dish(W, 1, [1, 2, 3])])
    plan.products[1] = p(1, 4.0, 2.0, "2e halve prijs")  # 2 packs: saves 2.00
    plan.products[2] = p(2, 3.0, 2.0, None)  # 1.00 off per piece
    plan.products[3] = p(3, 5.0, 5.0, None)  # no deal
    staple = Staple(p(30, 2.0, 1.0, None), 3, 1, 1, 1.0)
    return WeekLog(W, plan=plan.to_dict(), added={1: 2, 2: 1, 3: 1}, extras_added={30: 1},
                   staples=[staple.to_dict()], offers_added={40: 2}, offers_saved={40: 1.25},
                   savings=3.0, extras_savings=1.0, offers_savings=1.25, **kw)


def test_compute_counts_what_was_really_bought():
    log = week_log()
    # product 1: only 1 of the 2 (half price needs 2), 3 not bought, the offer complete
    entry = compute(log, {1: 1, 2: 1, 30: 2, 40: 2, 99: 5}, datetime(2026, 11, 12, 9, 0))
    assert entry["status"] == OK
    assert entry["bought_share"] == {"bought": 4, "added": 5}
    assert entry["realised_savings"] == 0.0 + 1.0 + 1.0 + 1.25
    assert entry["estimated"] == 5.25
    assert entry["at"] == "2026-11-12T09:00"


def test_due_day_is_the_day_after_the_delivery():
    assert due_day(WeekLog(W, delivery=date(2026, 11, 6))) == date(2026, 11, 7)
    assert due_day(WeekLog(W)) == W + timedelta(days=7)  # unknown: after the menu week
    assert due_day(WeekLog(W, delivery=date(2026, 12, 1))) == W + timedelta(days=7)


# --- the round: once per week, read only, never the total ---------------------------------


class Delivered(FakeAh):
    """FakeAh with delivered orders (read only) and a record of every call."""

    def __init__(self, tmp_path, orders=(), **kw):
        super().__init__(tmp_path, **kw)
        self.delivered = {o.id: o for o in orders}
        self.reads = []

    async def delivered_order(self, order_id):
        self._maybe_raise("delivered_order")
        self.reads.append(("order", order_id))
        return self.delivered.get(order_id)

    async def delivered_orders(self, start, end):
        self._maybe_raise("delivered_orders")
        self.reads.append(("week", start, end))
        return [o for o in self.delivered.values() if start <= o.day <= end]


def ctx_with_week(tmp_path, now, orders=(), **log_kw):
    ah = Delivered(tmp_path, orders)
    ctx = make_ctx(tmp_path, ah=ah, now=now)
    ctx.store.save(Tokens("a", "r", 9e9))
    ctx.ledger.save(week_log(**log_kw))
    return ctx


async def test_reconcile_runs_once_the_day_after_delivery(tmp_path):
    order = DeliveredOrder(5, date(2026, 11, 6), {1: 2, 2: 1, 30: 1})
    now = datetime(2026, 11, 6, 12, 0)
    ctx = ctx_with_week(tmp_path, now, [order], added_to="order:5",
                        delivery=date(2026, 11, 6))
    await reconcile.reconcile_due(ctx, now)
    assert ctx.client.reads == []  # the delivery day itself: not yet
    now = datetime(2026, 11, 7, 7, 0)
    await reconcile.reconcile_due(ctx, now)
    assert ctx.client.reads == []  # before schedule_time
    total = ctx.total_path.read_bytes() if ctx.total_path.exists() else None
    now = datetime(2026, 11, 7, 9, 0)
    await reconcile.reconcile_due(ctx, now)
    assert ctx.client.reads == [("order", 5)]
    got = ctx.reconciliations.load()[W.isoformat()]
    assert got["status"] == OK and got["bought_share"] == {"bought": 3, "added": 5}
    assert got["realised_savings"] == 4.0
    raw = json.loads((tmp_path / "reconciliation.json").read_text(encoding="utf-8"))
    assert raw["schema"] == RECON_SCHEMA == 1
    for hour in (10, 11, 15):
        await reconcile.reconcile_due(ctx, datetime(2026, 11, 7, hour, 0))
    await reconcile.reconcile_due(ctx, datetime(2026, 11, 8, 9, 0))
    assert ctx.client.reads == [("order", 5)]  # never again for this week
    assert ctx.client.writes == []  # read only
    after = ctx.total_path.read_bytes() if ctx.total_path.exists() else None
    assert after == total  # the cumulative total is never touched
    assert ctx.ledger.load(W).savings == 3.0


async def test_reconcile_from_the_list_looks_for_an_order_in_the_menu_week(tmp_path):
    order = DeliveredOrder(8, date(2026, 11, 9), {1: 2})
    now = datetime(2026, 11, 11, 9, 0)  # the Wednesday after the menu week
    ctx = ctx_with_week(tmp_path, now, [order], added_to="list")
    await reconcile.reconcile_due(ctx, now)
    assert ctx.client.reads == [("week", W, W + timedelta(days=6))]
    got = ctx.reconciliations.load()[W.isoformat()]
    assert got["bought_share"] == {"bought": 1, "added": 5}


async def test_no_order_or_a_till_receipt_is_niet_na_te_rekenen(tmp_path):
    now = datetime(2026, 11, 11, 9, 0)
    ctx = ctx_with_week(tmp_path, now, [], added_to="list")
    await reconcile.reconcile_due(ctx, now)
    got = ctx.reconciliations.load()[W.isoformat()]
    assert got["status"] == UNCHECKABLE and got["reason"]
    assert "bought_share" not in got and "realised_savings" not in got


async def test_reconcile_waits_while_writes_are_paused_or_the_login_is_gone(tmp_path):
    now = datetime(2026, 11, 11, 9, 0)
    ctx = ctx_with_week(tmp_path, now, [], added_to="list")
    ctx.health.failed("bonus", "vorm")
    ctx.health.failed("bonus", "vorm")
    await reconcile.reconcile_due(ctx, now)
    ctx.health.resume("test")
    ctx.login_expired = True
    await reconcile.reconcile_due(ctx, now)
    assert ctx.client.reads == [] and ctx.reconciliations.load() == {}


async def test_an_expired_login_found_on_the_way_does_not_use_up_the_week(tmp_path):
    now = datetime(2026, 11, 11, 9, 0)
    ctx = ctx_with_week(tmp_path, now, [], added_to="list")
    ctx.client.raise_on["ensure_fresh"] = AhLoginExpired("verlopen", 401)
    await reconcile.reconcile_due(ctx, now)
    assert ctx.login_expired is True and ctx.reconciliations.load() == {}


async def test_a_failing_read_is_marked_and_not_repeated(tmp_path):
    now = datetime(2026, 11, 11, 9, 0)
    ctx = ctx_with_week(tmp_path, now, [], added_to="list")
    ctx.client.raise_on["delivered_orders"] = AhApiError("boom", 500)
    await reconcile.reconcile_due(ctx, now)
    got = ctx.reconciliations.load()[W.isoformat()]
    assert got["status"] == UNCHECKABLE
    del ctx.client.raise_on["delivered_orders"]
    await reconcile.reconcile_due(ctx, datetime(2026, 11, 12, 9, 0))
    assert ctx.client.calls.count("delivered_orders") == 1


async def test_old_weeks_and_weeks_without_products_are_left_alone(tmp_path):
    now = datetime(2026, 12, 30, 9, 0)  # long after
    ctx = ctx_with_week(tmp_path, now, [], added_to="list")
    ctx.ledger.save(WeekLog(date(2026, 12, 16), plan=make_plan([]).to_dict()))
    await reconcile.reconcile_due(ctx, now)
    assert ctx.client.calls == [] and ctx.reconciliations.load() == {}


async def test_a_newer_reconciliation_file_is_never_written(tmp_path):
    now = datetime(2026, 11, 11, 9, 0)
    ctx = ctx_with_week(tmp_path, now, [], added_to="list")
    (tmp_path / "reconciliation.json").write_text('{"schema": 9, "weeks": {}}',
                                                  encoding="utf-8")
    await reconcile.reconcile_due(ctx, now)
    assert ctx.client.calls == [] and "reconciliation.json" in ctx.too_new


async def test_tick_runs_the_reconciliation_and_survives_its_failure(tmp_path, monkeypatch):
    now = datetime(2026, 11, 11, 9, 0)
    ctx = ctx_with_week(tmp_path, now, [DeliveredOrder(8, date(2026, 11, 9), {1: 2})],
                        added_to="list")
    ctx.ledger.save(WeekLog(main.menu_week_start(now), plan=make_plan([]).to_dict(),
                            round_done=True))
    await main.tick(ctx, now)
    assert W.isoformat() in ctx.reconciliations.load()

    async def boom(ctx, now):
        raise RuntimeError("x")
    monkeypatch.setattr(reconcile, "reconcile_due", boom)
    await main.tick(ctx, datetime(2026, 11, 11, 10, 0))  # logged, never raised


# --- the two numbers ---------------------------------------------------------------------------


def test_untouched_share_over_the_last_four_weeks(tmp_path):
    ctx = make_ctx(tmp_path)
    weeks = [W - timedelta(days=7 * i) for i in range(5)]  # W and the four before it
    for w in weeks:
        ctx.ledger.save(WeekLog(w, plan=make_plan([dish(w, 1, [1]), dish(w + timedelta(days=1),
                                                                            2, [2])]).to_dict()))
    # every recipe of make_plan has one line "1 stuk ui" (key "ui")
    at = datetime.combine(W - timedelta(days=5), time(9, 0))  # in the week before W
    ctx.overrides.put("ui", make_entry("skip", "1 stuk ui", None, at))
    got = untouched_share(ctx.ledger, ctx.overrides, ctx.history, W + timedelta(days=1))
    # last four weeks: W (override), W-7 (override made in that week), W-14, W-21
    assert got == {"weeks": 4, "lines": 8, "untouched": 4, "share": 0.5}
    # a "Klopt niet" taken back still counts as a correction in its week
    ctx.overrides.remove("ui")
    ctx.history.record(W - timedelta(days=21), W - timedelta(days=21), 1, "Recept 1",
                       "correct", at, key="ui")
    got = untouched_share(ctx.ledger, ctx.overrides, ctx.history, W + timedelta(days=1))
    assert got["untouched"] == 6 and got["lines"] == 8


def test_bought_share_over_the_last_four_reconciled_weeks(tmp_path):
    r = Reconciliations(tmp_path / "reconciliation.json")
    assert bought_share(r.load()) is None
    for i, (b, a) in enumerate([(9, 10), (8, 10), (1, 10), (10, 10), (5, 10)]):
        r.put((W - timedelta(days=7 * i)).isoformat(),
              {"status": OK, "bought_share": {"bought": b, "added": a}})
    r.put((W + timedelta(days=7)).isoformat(), {"status": UNCHECKABLE, "reason": "x"})
    assert bought_share(r.load()) == {"weeks": 4, "bought": 28, "added": 40, "share": 0.7}


async def test_savings_api_and_diagnose_show_both(aiohttp_client, tmp_path):
    now = datetime(2026, 11, 11, 9, 0)
    ctx = ctx_with_week(tmp_path, now, [DeliveredOrder(8, date(2026, 11, 9), {1: 2, 2: 1})],
                        added_to="list", counted=True)
    total = ctx.total_path.read_bytes() if ctx.total_path.exists() else None
    await reconcile.reconcile_due(ctx, now)
    client = await aiohttp_client(make_app(ctx))
    body = await (await client.get("api/savings")).json()
    week = next(w for w in body["weeks"] if w["week"] == W.isoformat())
    assert week["savings"] == 5.25  # the estimate stays
    assert week["reconciled"]["status"] == OK
    assert week["reconciled"]["realised_savings"] == 3.0
    assert body["metrics"]["bought"]["share"] == 0.4
    assert body["metrics"]["untouched"]["lines"] == 1
    d = diagnose(ctx)
    assert d["meetgetallen"]["bought"]["share"] == 0.4
    assert d["schemas"]["code"]["reconciliation.json"] == RECON_SCHEMA
    assert d["schemas"]["code"]["taste.json"] == 1
    after = ctx.total_path.read_bytes() if ctx.total_path.exists() else None
    assert after == total


async def test_the_week_logs_are_looked_at_once_an_hour(tmp_path, monkeypatch):
    now = datetime(2026, 11, 11, 9, 0)
    ctx = ctx_with_week(tmp_path, now, [], added_to="list")
    looks = []
    real = reconcile.week_to_check
    monkeypatch.setattr(reconcile, "week_to_check",
                        lambda *a: looks.append(1) or real(*a))
    ctx.reconciliations.put(W.isoformat(), {"status": OK})  # nothing due
    for minute in (0, 1, 30, 59):
        await reconcile.reconcile_due(ctx, now.replace(minute=minute))
    assert len(looks) == 1
    await reconcile.reconcile_due(ctx, now.replace(hour=10))
    assert len(looks) == 2
