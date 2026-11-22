"""v1.1: own recipes over the web API, and "Van een link" (import, not saved)."""

from pathlib import Path

import httpx
import pytest
import respx

from app import importer
from app.web import make_app
from tests.fakes import make_ctx

FIXTURE = Path(__file__).parent / "fixtures" / "recipe-page__leukerecepten.html"
PUBLIC = "93.184.215.14"


def body(**kw):
    return {"id": 0, "title": "Pasta pesto", "servings": 4,
            "ingredients": ["400 g pasta", "  ", "1 potje pesto"], "steps": ["Kook."],
            "cook": 20, "kind": None, **kw}


async def test_recipes_api_crud_and_validation(aiohttp_client, tmp_path):
    ctx = make_ctx(tmp_path)
    client = await aiohttp_client(make_app(ctx))

    r = await client.get("api/recipes")
    data = await r.json()
    assert data["items"] == [] and "Italiaans" in " ".join(data["kinds"])

    r = await client.post("api/recipes", json=body())
    assert r.status == 200
    saved = await r.json()
    assert saved["id"] == -1 and saved["ingredients"] == ["400 g pasta", "1 potje pesto"]
    assert saved["updated"] == ctx.clock().isoformat(timespec="minutes")
    assert ctx.recipes.get(-1).title == "Pasta pesto"

    r = await client.post("api/recipes", json=body(id=-1, title="Pasta met pesto",
                                                   kind=data["kinds"][0]))
    assert (await r.json())["title"] == "Pasta met pesto"
    assert [x["title"] for x in (await (await client.get("api/recipes")).json())["items"]] \
        == ["Pasta met pesto"]

    for bad, message in [
        (body(title="  "), "Geef het recept een naam"),
        (body(title=None), "Geef het recept een naam"),
        (body(servings=0), "Aantal personen moet tussen 1 en 20 liggen"),
        (body(servings=21), "Aantal personen moet tussen 1 en 20 liggen"),
        (body(servings="4"), "Aantal personen moet tussen 1 en 20 liggen"),
        (body(ingredients=["", " "]), "Zet er minstens één ingrediënt in"),
        (body(ingredients=None), "Zet er minstens één ingrediënt in"),
    ]:
        r = await client.post("api/recipes", json=bad)
        assert r.status == 400 and (await r.json())["error"] == message, bad

    for bad in [body(id=5), body(kind="Martiaans"), body(url="javascript:alert(1)"),
                body(image="data:image/png;base64,xx"), body(cook=-3), body(cook="lang")]:
        r = await client.post("api/recipes", json=bad)
        assert r.status == 400, bad
    r = await client.post("api/recipes", json=body(id=-9))
    assert r.status == 404
    assert len(ctx.recipes.all()) == 1

    r = await client.post("api/recipes/-1/remove")
    assert r.status == 200 and (await r.json())["items"] == []
    assert (await client.post("api/recipes/-1/remove")).status == 404
    assert (await client.post("api/recipes/abc/remove")).status == 400


@pytest.fixture
def fake_dns(monkeypatch):
    async def resolve(host, port):
        if host == "www.leukerecepten.nl":
            return [PUBLIC]
        raise importer.RecipeImportError(importer.NOT_FOUND)

    monkeypatch.setattr(importer, "resolve", resolve)


async def test_import_route_returns_unsaved_recipe(aiohttp_client, tmp_path, fake_dns):
    page = FIXTURE.read_text(encoding="utf-8")
    async with respx.mock(assert_all_called=False) as mock, httpx.AsyncClient() as http:
        mock.get(f"https://{PUBLIC}/recepten/ovenschotel/").respond(200, text=page)
        mock.get(f"https://{PUBLIC}/leeg/").respond(200, text="<html></html>")
        ctx = make_ctx(tmp_path, web_http=http)
        client = await aiohttp_client(make_app(ctx))

        r = await client.post("api/recipes/import",
                              json={"url": "https://www.leukerecepten.nl/recepten/ovenschotel/"})
        assert r.status == 200
        got = await r.json()
        assert got["id"] == 0 and got["title"] == "Ovenschotel met prei en kip"
        assert got["source"] == "leukerecepten.nl" and got["servings"] == 3
        assert ctx.recipes.all() == []  # the form saves it, not the import

        r = await client.post("api/recipes/import", json={"url": "http://127.0.0.1:8123/"})
        assert r.status == 422 and (await r.json())["error"]
        r = await client.post("api/recipes/import",
                              json={"url": "https://www.leukerecepten.nl/leeg/"})
        assert r.status == 422
        assert (await r.json())["error"] == "Op deze pagina staat geen recept dat ik kan lezen"
        r = await client.post("api/recipes/import", json={"url": 5})
        assert r.status == 400
        assert len(mock.calls) == 2


async def test_imports_run_one_at_a_time(aiohttp_client, tmp_path,
                                                             fake_dns, monkeypatch):
    import asyncio
    import threading
    import time

    from app import web as webmod

    monkeypatch.setattr(webmod, "PARSE_LIMIT", 2)
    running, most = 0, 0
    guard = threading.Lock()

    def slow_parse(html, url):
        nonlocal running, most
        with guard:
            running += 1
            most = max(most, running)
        time.sleep(0.2)
        with guard:
            running -= 1
        raise importer.RecipeImportError(importer.NO_RECIPE)

    monkeypatch.setattr(importer, "parse", slow_parse)
    async with respx.mock() as mock, httpx.AsyncClient() as http:
        mock.get(f"https://{PUBLIC}/r/").respond(200, text="<html></html>")
        client = await aiohttp_client(make_app(make_ctx(tmp_path, web_http=http)))
        link = {"url": "https://www.leukerecepten.nl/r/"}
        a, b = await asyncio.gather(client.post("api/recipes/import", json=link),
                                    client.post("api/recipes/import", json=link))
        for r in (a, b):
            assert r.status == 422
            assert (await r.json())["error"] == importer.NO_RECIPE
    assert most == 1


async def test_a_parse_that_never_ends_does_not_hang_the_next_import(
        aiohttp_client, tmp_path, fake_dns, monkeypatch):
    import threading

    from app import web as webmod

    monkeypatch.setattr(webmod, "PARSE_LIMIT", 0.2)
    release = threading.Event()

    def stuck_parse(html, url):
        release.wait(5)  # "never" within the test's patience
        raise importer.RecipeImportError(importer.NO_RECIPE)

    monkeypatch.setattr(importer, "parse", stuck_parse)
    async with respx.mock() as mock, httpx.AsyncClient() as http:
        mock.get(f"https://{PUBLIC}/r/").respond(200, text="<html></html>")
        client = await aiohttp_client(make_app(make_ctx(tmp_path, web_http=http)))
        link = {"url": "https://www.leukerecepten.nl/r/"}
        try:
            first = await client.post("api/recipes/import", json=link)
            assert first.status == 422  # the parse limit answered; its thread still runs
            assert (await first.json())["error"] == "Deze pagina is te lastig om te lezen"
            second = await client.post("api/recipes/import", json=link)
            assert second.status == 503
            assert (await second.json())["error"] == \
                "Er loopt nog een import; probeer het zo nog eens"
            assert len(mock.calls) == 1  # the second never fetched
        finally:
            release.set()
