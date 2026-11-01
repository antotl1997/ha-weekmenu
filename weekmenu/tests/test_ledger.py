import json
from datetime import date, timedelta

from app.ahclient import Product
from app.ledger import Ledger, WeekLog, leftovers
from app.planner import Plan

W = date(2026, 11, 4)  # a Wednesday


def prod(id, title="Gehakt"):
    return Product(id, title, None, 5.0, 3.0, True, None, True, None)


def plan(basket):
    return Plan([], basket, {p: prod(p) for p in basket}, 0.0, [], 0)


def with_dishes(week, ids):
    return WeekLog(week, plan={"dishes": [{"recipe": {"id": i}} for i in ids]})


def test_ledger_roundtrip_and_atomic(tmp_path):
    led = Ledger(tmp_path / "weeks")
    log = WeekLog(W, plan={"dishes": []}, added={5: 2}, target="list", savings=3.5,
                  counted=True, swapped_out={"2026-11-05": [1, 2]}, previous={"2026-11-05": 9},
                  extras_added={7: 1}, extras_savings=0.5)
    led.save(log)
    assert (tmp_path / "weeks" / "2026-11-04.json").exists()
    assert not list((tmp_path / "weeks").glob("*.tmp"))
    assert led.load(W) == log

    class Boom(WeekLog):
        def to_dict(self):
            raise RuntimeError("x")

    try:
        led.save(Boom(W))
    except RuntimeError:
        pass
    assert led.load(W) == log  # old file intact
    assert not list((tmp_path / "weeks").glob("*.tmp"))


def test_load_missing_week_gives_empty_log(tmp_path):
    log = Ledger(tmp_path).load(W)
    assert log == WeekLog(W) and log.added == {} and log.target is None


def test_load_older_file_without_extras_fields(tmp_path):
    (tmp_path / "2026-11-04.json").write_text(json.dumps({
        "week": "2026-11-04", "plan": None, "added": {"5": 1}, "target": "order",
        "savings": 1.0, "counted": False, "swapped_out": {}, "previous": {},
    }), encoding="utf-8")
    log = Ledger(tmp_path).load(W)
    assert log.added == {5: 1} and log.extras_added == {} and log.extras_savings == 0.0


def test_recent_recipe_ids_last_four_weeks(tmp_path):
    led = Ledger(tmp_path)
    for weeks_back, ids in [(1, [1, 2]), (4, [4]), (5, [5]), (0, [9]), (-1, [8])]:
        led.save(with_dishes(W - timedelta(days=7 * weeks_back), ids))
    assert led.recent_recipe_ids(W) == {1, 2, 4}
    assert led.recent_recipe_ids(W, weeks=1) == {1, 2}
    assert Ledger(tmp_path / "leeg").recent_recipe_ids(W) == set()


def test_weeks_to_count(tmp_path):
    led = Ledger(tmp_path)
    led.save(WeekLog(date(2026, 10, 28)))
    led.save(WeekLog(date(2026, 10, 21), counted=True))
    led.save(WeekLog(date(2026, 11, 11)))  # not begun yet
    got = led.weeks_to_count(date(2026, 11, 4))
    assert [g.week for g in got] == [date(2026, 10, 28)]
    assert Ledger(tmp_path / "leeg").weeks_to_count(W) == []


def test_leftovers_after_fewer_persons():
    log = WeekLog(W, added={1: 4, 2: 1, 3: 2})
    assert leftovers(log, plan({1: 2, 2: 1, 3: 3})) == {1: 2}
    assert leftovers(log, plan({})) == {1: 4, 2: 1, 3: 2}
