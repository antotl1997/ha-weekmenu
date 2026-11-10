import json
from datetime import date

import httpx
import pytest

from app.ahclient import Recipe
from app.hass import (
    BASE_URL,
    HELPER_AAN,
    HELPER_AVONDEN,
    Hass,
    format_eur,
    format_message,
    helper_persons,
    load_total,
    plan_view,
)
from app.planner import Dish, Plan
from app.writer import WriteResult


@pytest.fixture(autouse=True)
def _token(monkeypatch):
    monkeypatch.setenv("SUPERVISOR_TOKEN", "fake-token-for-tests")


def _recipe(title="Pasta pesto", url="https://example.test/r/1"):
    return Recipe(1, title, url, 20, None, None, [], [], 4, [], [], "https://example.test/i.jpg")


def _dish(d, title="Pasta pesto", savings=1.2, cook=20):
    return Dish(d, _recipe(title), "pasta", False, [], savings, persons=2, cook=cook)


def _plan(dishes=None, **kw):
    dishes = dishes if dishes is not None else [_dish(date(2026, 11, 4))]
    base = dict(
        dishes=dishes,
        basket={},
        products={},
        savings=sum(x.savings for x in dishes),
        not_found=[],
        short_by=0,
    )
    base.update(kw)
    return Plan(**base)


def _state(entity, state):
    return {"entity_id": entity, "state": state, "attributes": {}}


async def test_read_settings_maps_helpers(respx_mock):
    persons = helper_persons()
    values = {HELPER_AAN: "on", HELPER_AVONDEN: "4.0"}
    for i, eid in enumerate(persons.values()):
        values[eid] = f"{2 + (i % 2)}.0"
    values[persons["zaterdag"]] = "unavailable"
    for eid, st in values.items():
        respx_mock.get(f"{BASE_URL}/states/{eid}").respond(json=_state(eid, st))
    async with httpx.AsyncClient() as http:
        raw = await Hass(http).read_settings()
    assert raw.enabled is True
    assert raw.aantal == 4
    assert raw.persons["maandag"] == 2
    assert raw.persons["dinsdag"] == 3
    assert raw.persons["zaterdag"] == 2
    assert raw.defaulted == [persons["zaterdag"]]
    auth = respx_mock.calls[0].request.headers["Authorization"]
    assert auth == "Bearer fake-token-for-tests"


async def test_read_settings_defaults_when_missing(respx_mock):
    respx_mock.route(method="GET").respond(404, json={"message": "not found"})
    async with httpx.AsyncClient() as http:
        raw = await Hass(http).read_settings()
    assert raw.enabled is False
    assert raw.aantal == 5
    assert set(raw.persons.values()) == {2}
    assert HELPER_AAN in raw.defaulted
    assert len(raw.defaulted) == 9


async def test_publish_total_has_total_increasing(respx_mock, tmp_path):
    route = respx_mock.post(f"{BASE_URL}/states/sensor.weekmenu_besparing_totaal").respond(200, json={})
    path = tmp_path / "totaal.json"
    async with httpx.AsyncClient() as http:
        await Hass(http, total_path=path).publish_total(12.5)
    body = json.loads(route.calls[0].request.content)
    assert body["state"] == 12.5
    assert body["attributes"]["state_class"] == "total_increasing"
    assert body["attributes"]["unit_of_measurement"] == "€"
    assert load_total(path) == 12.5


def test_load_total_missing(tmp_path):
    assert load_total(tmp_path / "nope.json") == 0.0


async def test_publish_sets_both_sensors(respx_mock):
    r1 = respx_mock.post(f"{BASE_URL}/states/sensor.weekmenu").respond(200, json={})
    r2 = respx_mock.post(f"{BASE_URL}/states/sensor.weekmenu_besparing_week").respond(200, json={})
    view = plan_view(_plan(not_found=["nootmuskaat"]), date(2026, 11, 4), "list")
    async with httpx.AsyncClient() as http:
        await Hass(http).publish(view, 1.2)
    a = json.loads(r1.calls[0].request.content)
    assert a["state"] == 1
    assert a["attributes"]["week"] == "2026-11-04"
    assert a["attributes"]["doel"] == "lijst"
    assert a["attributes"]["niet_gevonden"] == ["nootmuskaat"]
    assert "ouders" not in a["attributes"]
    assert a["attributes"]["bestelling"] == "Voor de bestelling van woensdag 4 november"
    d = a["attributes"]["dishes"][0]
    assert d["datum"] == "2026-11-04" and d["dag"] == "woensdag" and d["titel"] == "Pasta pesto"
    assert d["kooktijd"] == 20 and d["keuken"] == "pasta" and d["personen"] == 2
    assert d["besparing"] == 1.2
    b = json.loads(r2.calls[0].request.content)
    assert b["attributes"]["device_class"] == "monetary"
    assert b["attributes"]["unit_of_measurement"] == "€"


async def test_set_setting_whitelist_and_services(respx_mock):
    num = respx_mock.post(f"{BASE_URL}/services/input_number/set_value").respond(200, json=[])
    on = respx_mock.post(f"{BASE_URL}/services/input_boolean/turn_on").respond(200, json=[])
    off = respx_mock.post(f"{BASE_URL}/services/input_boolean/turn_off").respond(200, json=[])
    async with httpx.AsyncClient() as http:
        h = Hass(http)
        await h.set_setting(HELPER_AVONDEN, 4)
        with pytest.raises(ValueError):
            await h.set_setting("input_select.weekmenu_ouders", "vrijdag")
        await h.set_setting(HELPER_AAN, True)
        await h.set_setting(HELPER_AAN, False)
        with pytest.raises(ValueError):
            await h.set_setting("light.keuken", True)
    assert json.loads(num.calls[0].request.content) == {"entity_id": HELPER_AVONDEN, "value": 4}
    assert on.called and off.called


async def test_notify_target_and_click_action(respx_mock):
    route = respx_mock.post(f"{BASE_URL}/services/notify/mobile_app_s_26_ultra_anto").respond(200, json=[])
    async with httpx.AsyncClient() as http:
        h = Hass(http)
        await h.notify("T", "M", "/lovelace/x")
        await h.notify("T", "M")
    first = json.loads(route.calls[0].request.content)
    assert first == {"title": "T", "message": "M", "data": {"clickAction": "/lovelace/x"}}
    assert "data" not in json.loads(route.calls[1].request.content)


def test_format_eur():
    assert format_eur(1.2) == "€ 1,20"
    assert format_eur(0) == "€ 0,00"
    assert format_eur(1234.5) == "€ 1234,50"


def test_message_mentions_target_and_leftovers():
    plan = _plan([_dish(date(2026, 11, 4)), _dish(date(2026, 11, 5), "Wraps", 0.5)])
    title, text = format_message(plan, WriteResult("order", leftovers=[("Melk", 1)]), None)
    assert title == "Weekmenu klaar · € 1,70 bespaard"
    assert "wo Pasta pesto (€ 1,20)" in text
    assert "do Wraps (€ 0,50)" in text
    assert "ouders" not in text and "niet thuis" not in text
    assert "in je bestelling" in text
    assert "Melk" in text


def test_message_has_no_parents_text_and_names_away_evenings():
    _, text = format_message(_plan(), WriteResult("list"), None,
                             away=[date(2026, 11, 7), date(2026, 11, 9)])
    assert "ouders" not in text
    assert "za niet thuis" in text and "ma niet thuis" in text
    assert "op Mijn lijst" in text


def test_message_includes_period_warning():
    _, text = format_message(_plan(), WriteResult("order"), "Folder nog niet zichtbaar")
    assert "Folder nog niet zichtbaar" in text


def test_message_extra_lines():
    plan = _plan(not_found=["snufje nootmuskaat"], short_by=2, budget_exhausted=True)
    result = WriteResult("order", unknown=[("Kaas", 1), ("Pesto", 2)])
    _, text = format_message(plan, result, None)
    assert "Controleer in de AH-app: Kaas, Pesto" in text
    assert "Te weinig recepten voor 2 avonden" in text
    assert "Zoeken ingekort" in text
    assert "snufje nootmuskaat" in text
    _, one = format_message(_plan(short_by=1), WriteResult("order"), None)
    assert "Te weinig recepten voor 1 avond" in one
    assert "1 avonden" not in one


def test_message_opens_with_the_order_sentence():
    _, text = format_message(_plan(), WriteResult("order"), "Let op",
                             start=date(2026, 11, 11))
    lines = text.splitlines()
    assert lines[0] == "Voor de bestelling van woensdag 11 november"
    assert lines[1] == "Let op"


async def test_read_settings_zero_persons_stays_zero(respx_mock):
    persons = helper_persons()
    eid = persons["vrijdag"]
    respx_mock.get(f"{BASE_URL}/states/{eid}").respond(json=_state(eid, "0.0"))
    respx_mock.route(method="GET").respond(404, json={"message": "not found"})
    async with httpx.AsyncClient() as http:
        raw = await Hass(http).read_settings()
    assert raw.persons["vrijdag"] == 0 and raw.persons["maandag"] == 2
    assert eid not in raw.defaulted
    assert raw.away == {"vrijdag"}


async def test_set_setting_accepts_zero_persons(respx_mock):
    route = respx_mock.post(f"{BASE_URL}/services/input_number/set_value").respond(200, json={})
    eid = helper_persons()["vrijdag"]
    async with httpx.AsyncClient() as http:
        await Hass(http).set_setting(eid, 0)
    assert json.loads(route.calls[0].request.content) == {"entity_id": eid, "value": 0}


def test_message_names_the_days_not_at_home():
    away = [date(2026, 11, 6), date(2026, 11, 9)]
    _, text = format_message(_plan(), WriteResult("order"), None, away=away)
    assert "vr niet thuis" in text and "ma niet thuis" in text
    _, text = format_message(_plan(), WriteResult("order"), None)
    assert "niet thuis" not in text


def test_plan_view_marks_the_evenings_without_food():
    view = plan_view(_plan(), date(2026, 11, 4), "order",
                     away=[date(2026, 11, 6)])
    assert view["niet_thuis"] == ["2026-11-06"]
    assert plan_view(_plan(), date(2026, 11, 4), "order")["niet_thuis"] == []


def test_plan_view_and_message_skip_away_days_that_have_a_dish():
    plan = _plan([_dish(date(2026, 11, 6))])
    away = [date(2026, 11, 6), date(2026, 11, 9)]
    view = plan_view(plan, date(2026, 11, 4), "order", away=away)
    assert view["niet_thuis"] == ["2026-11-09"]
    # a reused plan still has a dish on vr: the message does not call it niet thuis
    _, text = format_message(plan, WriteResult("order"), None, away=away)
    assert "vr niet thuis" not in text and "ma niet thuis" in text


def test_raw_settings_away_is_every_day_at_zero():
    from app.hass import RawSettings
    raw = RawSettings(True, 5, {"vrijdag": 0, "zaterdag": 0, "maandag": 2})
    assert raw.away == {"vrijdag", "zaterdag"}


# --- Niet thuis eten: the add-on remembers the number from before -----------------------


def _fake(tmp_path):
    from tests.fakes import FakeHass
    return FakeHass(tmp_path / "totaal.json")


async def test_set_away_remembers_the_old_number_and_restores_it(tmp_path):
    hass = _fake(tmp_path)
    eid = helper_persons()["vrijdag"]
    hass.states[eid] = "4"
    assert await hass.set_away("vrijdag", True) == 0
    assert hass.posts[-1] == ("services/input_number/set_value", {"entity_id": eid, "value": 0})
    hass.states[eid] = "0.0"  # what Home Assistant now says
    assert await hass.set_away("vrijdag", False) == 4
    assert hass.posts[-1] == ("services/input_number/set_value", {"entity_id": eid, "value": 4})


async def test_set_away_restore_survives_a_restart(tmp_path):
    first = _fake(tmp_path)
    eid = helper_persons()["zaterdag"]
    first.states[eid] = "3"
    await first.set_away("zaterdag", True)
    second = _fake(tmp_path)  # the add-on restarted: same /data
    second.states[eid] = "0"
    assert await second.set_away("zaterdag", False) == 3


async def test_set_away_off_without_memory_gives_the_default(tmp_path):
    hass = _fake(tmp_path)
    eid = helper_persons()["zondag"]
    hass.states[eid] = "0"  # set to 0 in Home Assistant itself
    assert await hass.set_away("zondag", False) == 2


async def test_set_away_off_leaves_a_day_that_is_not_away(tmp_path):
    hass = _fake(tmp_path)
    eid = helper_persons()["maandag"]
    hass.states[eid] = "5"
    assert await hass.set_away("maandag", False) == 5
    assert hass.posts == []


async def test_set_away_twice_keeps_the_first_number(tmp_path):
    hass = _fake(tmp_path)
    eid = helper_persons()["dinsdag"]
    hass.states[eid] = "6"
    await hass.set_away("dinsdag", True)
    hass.states[eid] = "0"
    await hass.set_away("dinsdag", True)
    assert await hass.set_away("dinsdag", False) == 6


async def test_set_away_rejects_a_non_weekday(tmp_path):
    with pytest.raises(ValueError):
        await _fake(tmp_path).set_away("light.keuken", True)


def test_plan_view_and_message_name_the_days_without_a_dish():
    plan = _plan([_dish(date(2026, 11, 4))], removed_days=[date(2026, 11, 5)])
    view = plan_view(plan, date(2026, 11, 4), "order")
    assert view["geen_gerecht"] == ["2026-11-05"] and view["niet_thuis"] == []
    _, text = format_message(plan, WriteResult("order"), None)
    assert "do geen gerecht" in text
    assert plan_view(_plan(), date(2026, 11, 4), "list")["geen_gerecht"] == []


def test_message_names_what_was_cleaned_up():
    _, text = format_message(_plan(), WriteResult("order", removed={1: 1, 2: 2}), None)
    assert "2 producten uit je bestelling gehaald" in text
    _, text = format_message(_plan(), WriteResult("list", removed={1: 1}), None)
    assert "1 product van je lijst gehaald" in text
    _, text = format_message(_plan(), WriteResult("list"), None)
    assert "gehaald" not in text
