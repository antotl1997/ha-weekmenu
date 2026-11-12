"""The bonus folder is read once per period: several periods kept, also over a restart."""

import json
import time
from datetime import date

from app import main
from app.ahclient import AhApiError, Product
from app.foldercache import KEEP, load_folders, remember, save_folders
from app.tokens import Tokens
from app.web import make_app
from tests.fakes import SUNDAY, W, FakeAh, make_ctx


def p(pid, title, image=None):
    return Product(pid, title, "1 kg", 3.0, 2.0, True, "1 + 1 gratis", True, "Vlees",
                   hq_id=None, image=image)


def logged_in(ah):
    ah.store.save(Tokens("a", "r", time.time() + 3600))
    return ah


def test_remember_keeps_the_newest_periods():
    cache = {}
    for i in range(KEEP + 2):
        cache = remember(cache, date(2026, 11, 2 + 7 * i), [p(i, f"P{i}")])
    assert len(cache) == KEEP
    assert min(cache) == date(2026, 11, 2 + 7 * 2)


def test_save_and_load_keep_order_and_photos(tmp_path):
    path = tmp_path / "folder.json"
    folder = [p(9, "Zalm", "https://example.invalid/z.jpg"), p(3, "Appel")]
    save_folders(path, {date(2026, 11, 2): folder})
    assert json.loads(path.read_text(encoding="utf-8"))["schema"] == 1
    assert load_folders(path) == {date(2026, 11, 2): folder}


def test_a_broken_or_newer_file_loads_as_empty(tmp_path):
    path = tmp_path / "folder.json"
    assert load_folders(path) == {}  # no file yet
    path.write_text("{niet json", encoding="utf-8")
    assert load_folders(path) == {}
    path.write_text(json.dumps({"schema": 99, "folders": {}}), encoding="utf-8")
    assert load_folders(path) == {}
    path.write_text(json.dumps({"schema": 1, "folders": {"2026-11-02": [{"x": 1}]}}),
                    encoding="utf-8")
    assert load_folders(path) == {}  # a broken product: that folder is read again


async def test_two_weeks_do_not_push_each_other_out(tmp_path):
    ctx = make_ctx(tmp_path)
    await main.folder_for_week(ctx, W, SUNDAY, fallback=False)  # next week's folder
    await main.folder_for_week(ctx, main.week_of(SUNDAY.date()), SUNDAY, fallback=False)
    await main.folder_for_week(ctx, W, SUNDAY, fallback=False)
    assert ctx.client.calls.count("bonus_offers") == 2  # not 3: both stayed


async def test_the_folder_survives_a_restart(aiohttp_client, tmp_path):
    ctx = make_ctx(tmp_path, folder_path=tmp_path / "folder.json")
    await main.folder_for_week(ctx, W, SUNDAY, fallback=False)
    assert (tmp_path / "folder.json").exists()

    again = make_ctx(tmp_path, ah=FakeAh(tmp_path), folder_path=tmp_path / "folder.json")
    again.bonus_cache = load_folders(again.folder_path)  # what amain does at start
    client = await aiohttp_client(make_app(again))
    body = await (await client.get("api/offers")).json()
    assert [i["id"] for c in body["categories"] for i in c["items"]] == [1, 2]
    assert again.client.calls.count("bonus_offers") == 0


async def test_warm_folder_reads_the_shown_week_once(tmp_path):
    ctx = make_ctx(tmp_path, ah=logged_in(FakeAh(tmp_path)))
    await main.warm_folder(ctx)
    await main.warm_folder(ctx)  # kept now: nothing read again
    assert ctx.client.calls.count("bonus_offers") == 1
    assert [x.start for x in ctx.client.offers_for] == [date(2026, 11, 2)]


async def test_warm_folder_needs_a_login_and_never_raises(tmp_path):
    ctx = make_ctx(tmp_path)  # no tokens.json
    await main.warm_folder(ctx)
    assert "bonus_offers" not in ctx.client.calls

    ah = logged_in(FakeAh(tmp_path))
    ah.raise_on["bonus_offers"] = AhApiError("boom", 500)
    ctx = make_ctx(tmp_path, ah=ah)
    await main.warm_folder(ctx)  # logged, left to the tab
    assert ctx.bonus_cache == {} and not ctx.lock.locked()
