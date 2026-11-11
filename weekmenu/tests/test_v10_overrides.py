"""v1.0 point 6 (review of 0.9): Terugdraaien publishes what it already changed, and a
failing re-pick gives one clear message instead of an error."""

from app.ahclient import AhApiError
from app.web import REPICK_FAILED, make_app
from tests.fakes import FakeAh
from tests.test_v09_web import WO, p, planned


def sensor_posts(ctx):
    return [x for path, x in ctx.hass.posts if path == "states/sensor.weekmenu"]


async def test_terugdraaien_publishes_a_week_whose_write_failed(aiohttp_client, tmp_path):
    # writes 0 and 1: the menu; 2: the lowering of "Hebben we al"; 3: the re-pick fails
    ctx = await planned(tmp_path, ah=FakeAh(tmp_path, fail_on_call=3))
    client = await aiohttp_client(make_app(ctx))
    r = await client.post(f"api/dish/{WO}/correct",
                          json={"ingredient": "300 g kipfilet", "kind": "pantry"})
    assert r.status == 200
    ctx.client.search_results["kipfilet"] = [p(2, "AH Kipfilet", "300 g")]
    before = len(sensor_posts(ctx))
    r = await client.post("api/overrides/remove", json={"key": "kipfilet"})
    assert r.status == 200, await r.text()
    body = await r.json()
    assert body["warning"] == REPICK_FAILED
    assert body["items"] == [] and ctx.overrides.load() == {}
    # apply saved the new plan before the write failed: the sensor shows it anyway
    assert len(sensor_posts(ctx)) == before + 1
    log = ctx.ledger.load(WO)
    assert 2 in log.plan["basket"] or "2" in log.plan["basket"]
    assert 2 in log.pending  # never retried blind: settled by the next apply


async def test_terugdraaien_when_choosing_again_fails(aiohttp_client, tmp_path):
    ctx = await planned(tmp_path)
    client = await aiohttp_client(make_app(ctx))
    await client.post(f"api/dish/{WO}/correct",
                      json={"ingredient": "300 g kipfilet", "kind": "pantry"})
    writes = list(ctx.client.writes)
    ctx.client.raise_on["search_products"] = AhApiError("boom", 500)
    r = await client.post("api/overrides/remove", json={"key": "kipfilet"})
    assert r.status == 200, await r.text()
    body = await r.json()
    assert body["warning"] == REPICK_FAILED
    assert REPICK_FAILED == "Je keuze is weg, het menu wordt bij de volgende ronde aangepast"
    assert ctx.overrides.load() == {} and ctx.client.writes == writes


async def test_terugdraaien_without_trouble_has_no_warning(aiohttp_client, tmp_path):
    ctx = await planned(tmp_path)
    client = await aiohttp_client(make_app(ctx))
    await client.post(f"api/dish/{WO}/correct",
                      json={"ingredient": "300 g kipfilet", "kind": "pantry"})
    ctx.client.search_results["kipfilet"] = [p(2, "AH Kipfilet", "300 g")]
    body = await (await client.post("api/overrides/remove", json={"key": "kipfilet"})).json()
    assert body["warning"] is None
