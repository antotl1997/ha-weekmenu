"""v1.0 point 2: duim omlaag and "nooit meer" per dish, in /data/taste.json."""

import json
from datetime import datetime

from app import main
from app.taste import DOWN, NEVER, TASTE_SCHEMA, Taste
from app.tokens import Tokens
from app.web import make_app
from app.writer import apply
from tests.fakes import W, dish, make_ctx, make_plan

AT = datetime(2026, 11, 1, 9, 30)


# --- the file ---------------------------------------------------------------------------


def test_taste_file_keeps_one_choice_per_recipe(tmp_path):
    t = Taste(tmp_path / "taste.json")
    assert t.load() == {} and t.never() == set() and t.down() == set()
    t.put(60, DOWN, "Stamppot met gehakt", AT)
    t.put(61, NEVER, "Nasi met kip", AT)
    assert t.down() == {60} and t.never() == {61}
    t.put(60, NEVER, "Stamppot met gehakt", AT)  # nooit meer goes over the thumb
    assert t.down() == set() and t.never() == {60, 61}
    raw = json.loads((tmp_path / "taste.json").read_text(encoding="utf-8"))
    assert raw["schema"] == TASTE_SCHEMA == 1
    assert raw["recipes"]["61"] == {"kind": "never", "title": "Nasi met kip",
                                    "at": "2026-11-01T09:30"}
    assert t.remove(61) is True and t.remove(61) is False
    assert [x["recipe_id"] for x in t.listing()] == [60]


def test_taste_file_broken_or_newer(tmp_path):
    path = tmp_path / "taste.json"
    path.write_text("{kapot", encoding="utf-8")
    assert Taste(path).load() == {}
    path.write_text(json.dumps({"schema": 1, "recipes": {
        "7": {"kind": "never", "title": "x", "at": "2026-11-01T09:30"},
        "8": {"kind": "raar"}, "x": {"kind": "down"}}}), encoding="utf-8")
    assert Taste(path).never() == {7} and Taste(path).down() == set()
    path.write_text(json.dumps({"schema": 9, "recipes": {}}), encoding="utf-8")
    assert Taste(path).too_new() is True


# --- the settings --------------------------------------------------------------------------


def test_make_settings_carries_the_taste(tmp_path):
    ctx = make_ctx(tmp_path)
    ctx.taste.put(60, NEVER, "a", AT)
    ctx.taste.put(61, DOWN, "b", AT)
    raw = main.RawSettings(True, 3, {d: 2 for d in main.WEEKDAGEN})
    s = main.make_settings(ctx, raw, W)
    assert s.never == {60} and s.down == {61}


# --- the web API ----------------------------------------------------------------------------


async def planned(tmp_path):
    ctx = make_ctx(tmp_path)
    ctx.store.save(Tokens("test-access", "test-refresh", 9e9))
    await apply(ctx.client, ctx.ledger, ctx.ledger.load(W), make_plan([dish(W, 7, [1])]))
    return ctx


async def test_thumb_and_never_from_the_dish_screen(aiohttp_client, tmp_path):
    ctx = await planned(tmp_path)
    client = await aiohttp_client(make_app(ctx))
    d = await (await client.get(f"api/dish/{W}")).json()
    assert d["taste"] is None
    r = await client.post(f"api/dish/{W}/taste", json={"kind": "down"})
    assert r.status == 200 and (await r.json())["taste"] == "down"
    assert ctx.taste.down() == {7}
    r = await client.post(f"api/dish/{W}/taste", json={"kind": "never"})
    assert (await r.json())["taste"] == "never" and ctx.taste.never() == {7}
    assert (await (await client.get(f"api/dish/{W}")).json())["taste"] == "never"
    listing = await (await client.get("api/taste")).json()
    assert listing["items"] == [{"recipe_id": 7, "title": "Recept 7", "kind": "never",
                                 "at": "2026-11-01T08:00"}]
    r = await client.post(f"api/dish/{W}/taste", json={"kind": None})
    assert (await r.json())["taste"] is None and ctx.taste.load() == {}
    # nothing went to AH for any of this
    assert all(name not in ("add", "reduce") for name in ctx.client.calls)


async def test_nooit_meer_list_takes_one_back(aiohttp_client, tmp_path):
    ctx = await planned(tmp_path)
    ctx.taste.put(9, NEVER, "Oude lasagne", AT)
    ctx.taste.put(8, DOWN, "Duim", AT)
    client = await aiohttp_client(make_app(ctx))
    items = (await (await client.get("api/taste")).json())["items"]
    assert [x["recipe_id"] for x in items] == [9]  # only "nooit meer" is listed
    r = await client.post("api/taste/remove", json={"recipe_id": 9})
    assert r.status == 200 and (await r.json())["items"] == []
    assert ctx.taste.never() == set() and ctx.taste.down() == {8}
    r = await client.post("api/taste/remove", json={"recipe_id": 9})
    assert r.status == 404


async def test_taste_refuses_what_it_cannot_check(aiohttp_client, tmp_path):
    ctx = await planned(tmp_path)
    client = await aiohttp_client(make_app(ctx))
    assert (await client.post(f"api/dish/{W}/taste", json={"kind": "raar"})).status == 400
    assert (await client.post("api/dish/2026-11-05/taste", json={"kind": "down"})).status == 404
    assert (await client.post("api/taste/remove", json={"recipe_id": "9"})).status == 400
    ctx.in_round = True  # never wait for a whole menu round for a thumb
    assert (await client.post(f"api/dish/{W}/taste", json={"kind": "down"})).status == 409
    ctx.in_round = False
    ctx.taste.path.write_text(json.dumps({"schema": 9, "recipes": {}}), encoding="utf-8")
    r = await client.post(f"api/dish/{W}/taste", json={"kind": "down"})
    assert r.status == 409  # a newer add-on's file is never written
    assert json.loads(ctx.taste.path.read_text(encoding="utf-8"))["schema"] == 9
