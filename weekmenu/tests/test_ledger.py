import json
from datetime import date, timedelta

import pytest

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
                  extras_added={7: 1}, extras_savings=0.5,
                  pending={3: {"before": 1, "delta": 2, "target": "order", "order_id": 8}},
                  unknown={4: 1})
    led.save(log)
    assert (tmp_path / "weeks" / "2026-11-04.json").exists()
    assert not list((tmp_path / "weeks").glob("*.tmp"))
    assert led.load(W) == log

    class Boom(WeekLog):
        def to_dict(self):
            raise RuntimeError("x")

    with pytest.raises(RuntimeError):
        led.save(Boom(W))
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
    assert log.pending == {} and log.unknown == {}


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


def test_staples_fields_roundtrip_and_default(tmp_path):
    led = Ledger(tmp_path)
    log = WeekLog(W, staples=[{"product": {"id": 1}, "times": 2}], staples_error="HTTP 500")
    led.save(log)
    assert led.load(W) == log
    (tmp_path / "2026-11-04.json").write_text(json.dumps({"week": "2026-11-04"}),
                                              encoding="utf-8")
    old = led.load(W)
    assert old.staples == [] and old.staples_error is None


# --- final review fixes: the directory is fsynced after the replace -----------------


def test_fsync_dir_after_replace(tmp_path, monkeypatch):
    from app import durable
    synced, closed = [], []
    real_open = durable.os.open

    def fake_open(path, flags, *a, **kw):
        if path == str(tmp_path / "weeks"):
            return 9999
        return real_open(path, flags, *a, **kw)

    real_fsync = durable.os.fsync
    monkeypatch.setattr(durable.os, "open", fake_open)
    monkeypatch.setattr(durable.os, "fsync",
                        lambda fd: synced.append(fd) if fd == 9999 else real_fsync(fd))
    monkeypatch.setattr(durable.os, "close", lambda fd: closed.append(fd))
    led = Ledger(tmp_path / "weeks")
    led.save(WeekLog(date(2026, 11, 4), savings=1.5))
    assert synced == [9999] and closed == [9999]
    assert led.load(date(2026, 11, 4)).savings == 1.5


def test_save_works_when_the_directory_cannot_be_opened(tmp_path, monkeypatch):
    """Windows cannot open a directory: the save still succeeds."""
    from app import durable

    real_open = durable.os.open

    def refuse(path, flags, *a, **kw):
        if path == str(tmp_path / "weeks"):  # only the directory (the tmp file opens)
            raise PermissionError(13, "Permission denied")
        return real_open(path, flags, *a, **kw)

    led = Ledger(tmp_path / "weeks")
    monkeypatch.setattr(durable.os, "open", refuse)
    led.save(WeekLog(date(2026, 11, 4), savings=2.0))
    assert led.load(date(2026, 11, 4)).savings == 2.0


def test_tokens_and_total_fsync_their_directory(tmp_path, monkeypatch):
    from app.hass import _save_total, load_total
    from app.tokens import Tokens, TokenStore
    dirs = []
    monkeypatch.setattr("app.durable.fsync_dir", lambda d: dirs.append(str(d)))
    monkeypatch.setattr("app.hass.fsync_dir", lambda d: dirs.append(str(d)))
    TokenStore(tmp_path / "t" / "tokens.json").save(Tokens("a", "r", 1.0))
    _save_total(tmp_path / "s" / "totaal.json", 3.5, {})
    assert dirs == [str(tmp_path / "t"), str(tmp_path / "s")]
    assert load_total(tmp_path / "s" / "totaal.json") == 3.5


# --- v0.4 C: menu before the new folder ----------------------------------------------------


def test_folder_fields_roundtrip_and_old_file_loads(tmp_path):
    from app.ledger import new_folder_available
    led = Ledger(tmp_path)
    log = WeekLog(W, plan={"dishes": []}, folder_period_start=date(2026, 10, 26),
                  new_folder=date(2026, 11, 2), new_folder_notified=True)
    led.save(log)
    assert led.load(W) == log
    assert new_folder_available(log)
    # the menu was remade with the new folder: nothing new any more
    log.folder_period_start = date(2026, 11, 2)
    assert not new_folder_available(log)
    # a log from before these fields
    (tmp_path / "2026-11-11.json").write_text(json.dumps({
        "week": "2026-11-11", "plan": {"dishes": []}, "added": {}, "target": "list",
        "savings": 0.0, "counted": False, "swapped_out": {}, "previous": {},
    }), encoding="utf-8")
    old = led.load(date(2026, 11, 11))
    assert old.folder_period_start is None and old.new_folder is None
    assert old.new_folder_notified is False and not new_folder_available(old)
    # no menu: never "bijwerken"
    assert not new_folder_available(WeekLog(W, new_folder=date(2026, 11, 2)))


def test_removed_by_user_round_trips_and_old_logs_load(tmp_path):
    from datetime import date as _date
    from app.ledger import Ledger as _Ledger, WeekLog as _WeekLog
    led = _Ledger(tmp_path)
    log = _WeekLog(_date(2026, 11, 4), removed_by_user={5: 2})
    led.save(log)
    assert led.load(_date(2026, 11, 4)).removed_by_user == {5: 2}
    old = log.to_dict()
    del old["removed_by_user"]  # a log from before 0.4.0's fix
    assert _WeekLog.from_dict(old).removed_by_user == {}


# --- v0.6: schema versions and durable writes ------------------------------------------


def test_weeklog_migration_v0_to_v1_keeps_every_field():
    from app.ledger import migrate_weeklog_v0_to_v1
    old = {"week": "2026-11-04", "added": {"5": 1}, "target": "order"}
    new = migrate_weeklog_v0_to_v1(old)
    assert new == {**old, "schema": 1}
    assert WeekLog.from_dict(new) == WeekLog.from_dict(old)


def test_saved_log_has_schema_and_old_log_is_kept_once_as_bak(tmp_path):
    led = Ledger(tmp_path)
    old = {"week": "2026-11-04", "plan": None, "added": {"5": 1}, "target": "order"}
    (tmp_path / "2026-11-04.json").write_text(json.dumps(old), encoding="utf-8")
    log = led.load(W)
    assert log.added == {5: 1} and not log.too_new
    log.added[6] = 2
    led.save(log)
    data = json.loads((tmp_path / "2026-11-04.json").read_text(encoding="utf-8"))
    assert data["schema"] == 1 and data["added"] == {"5": 1, "6": 2}
    assert json.loads((tmp_path / "2026-11-04.json.bak").read_text(encoding="utf-8")) == old
    led.save(log)  # once
    assert json.loads((tmp_path / "2026-11-04.json.bak").read_text(encoding="utf-8")) == old
    # the backup is not a week log
    assert [w.week for w in led.weeks()] == [W]


def test_too_new_log_loads_for_display_and_is_never_written(tmp_path):
    from app.durable import SchemaTooNew
    led = Ledger(tmp_path)
    newer = {"schema": 99, "week": "2026-11-04", "plan": None, "added": {"5": 1},
             "iets_nieuws": True}
    path = tmp_path / "2026-11-04.json"
    path.write_text(json.dumps(newer), encoding="utf-8")
    before = path.read_bytes()
    log = led.load(W)
    assert log.too_new and log.added == {5: 1}  # reading for display is fine
    log.added[6] = 1
    with pytest.raises(SchemaTooNew):
        led.save(log)
    with pytest.raises(SchemaTooNew):  # a fresh WeekLog over it is refused too
        led.save(WeekLog(W))
    assert path.read_bytes() == before
    assert not list(tmp_path.glob("*.bak")) and not list(tmp_path.glob("*.tmp"))


def test_too_new_log_that_does_not_parse_loads_empty(tmp_path):
    (tmp_path / "2026-11-04.json").write_text(
        json.dumps({"schema": 7, "week": "anders", "added": "x"}), encoding="utf-8")
    log = Ledger(tmp_path).load(W)
    assert log.too_new and log.week == W and log.added == {}
