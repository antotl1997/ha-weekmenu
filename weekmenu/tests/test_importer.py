"""v1.1: a recipe from a link. The fetch guard (never inward) and the parse."""

import asyncio
from pathlib import Path

import httpx
import pytest
import respx

from app import importer
from app.importer import RecipeImportError, fetch_page, parse

FIXTURE = Path(__file__).parent / "fixtures" / "recipe-page__leukerecepten.html"
PUBLIC = "93.184.215.14"  # stands in for a public web server; never contacted
NAMES = {
    "recepten.example.com": [PUBLIC],
    "localhost": ["127.0.0.1"],
    "supervisor": ["172.30.32.2"],
    "binnen.example.com": [PUBLIC, "192.168.1.10"],  # one inward answer is enough
    "mapped.example.com": ["::ffff:127.0.0.1"],
}


@pytest.fixture(autouse=True)
def fake_dns(monkeypatch):
    async def resolve(host: str, port: int) -> list[str]:
        if host not in NAMES:
            raise RecipeImportError(importer.NOT_FOUND)
        return NAMES[host]

    monkeypatch.setattr(importer, "resolve", resolve)


def test_parse_reads_json_ld():
    r = parse(FIXTURE.read_text(encoding="utf-8"),
              "https://www.leukerecepten.nl/recepten/ovenschotel-prei-kip/")
    assert r.id == 0
    assert r.title == "Ovenschotel met prei en kip"
    assert r.servings == 3
    assert r.ingredients == ["400 g kipfilet", "2 preien", "750 g kruimige aardappelen",
                             "1 el olijfolie", "100 g geraspte kaas"]
    assert len(r.steps) == 3 and r.steps[0] == "Schil de aardappelen en kook ze gaar."
    assert r.cook == 45
    assert r.source == "leukerecepten.nl"
    assert r.url == "https://www.leukerecepten.nl/recepten/ovenschotel-prei-kip/"
    assert r.image == "https://images.example.invalid/ovenschotel-prei-kip.jpg"


def test_parse_without_a_recipe_says_so():
    with pytest.raises(RecipeImportError) as e:
        parse("<html><body><p>Geen recept</p></body></html>", "https://example.com/x")
    assert e.value.message == "Op deze pagina staat geen recept dat ik kan lezen"


@pytest.mark.parametrize("url", [
    "http://localhost:8123/",
    "http://127.0.0.1/",
    "http://192.168.1.10/",
    "http://10.0.0.1/",
    "http://169.254.169.254/",
    "http://supervisor/core/api",
    "ftp://example.com/",
    "http://[::1]/",
    "http://[::ffff:127.0.0.1]/",
    "http://mapped.example.com/",
    "http://binnen.example.com/",
    "http://0.0.0.0/",
    "http://user:geheim@recepten.example.com/",
    "http://recepten.example.com:8123/",
    "javascript:alert(1)",
    "recepten.example.com/zonder-schema",
    "",
])
async def test_fetch_refuses_inward_addresses(url):
    async with respx.mock(assert_all_called=False) as mock, httpx.AsyncClient() as http:
        with pytest.raises(RecipeImportError) as e:
            await fetch_page(http, url)
        assert not mock.calls
    assert e.value.message


async def test_fetch_pins_the_checked_address_and_keeps_the_name():
    async with respx.mock() as mock, httpx.AsyncClient() as http:
        route = mock.get(f"https://{PUBLIC}/recept").respond(200, text="<html>ok</html>")
        final, html = await fetch_page(http, "https://recepten.example.com/recept")
    assert (final, html) == ("https://recepten.example.com/recept", "<html>ok</html>")
    req = route.calls.last.request
    assert req.headers["host"] == "recepten.example.com"
    assert req.extensions["sni_hostname"] == "recepten.example.com"


async def test_fetch_refuses_a_redirect_inward():
    async with respx.mock(assert_all_called=False) as mock, httpx.AsyncClient() as http:
        mock.get(f"http://{PUBLIC}/r").respond(302, headers={"Location": "http://127.0.0.1/"})
        inward = mock.get("http://127.0.0.1/").respond(200, text="geheim")
        with pytest.raises(RecipeImportError):
            await fetch_page(http, "http://recepten.example.com/r")
        assert len(mock.calls) == 1 and not inward.called


async def test_fetch_follows_a_public_redirect_relative_to_the_name():
    async with respx.mock() as mock, httpx.AsyncClient() as http:
        mock.get(f"http://{PUBLIC}/oud").respond(301, headers={"Location": "/nieuw"})
        mock.get(f"http://{PUBLIC}/nieuw").respond(200, text="<html>hier</html>")
        final, html = await fetch_page(http, "http://recepten.example.com/oud")
    assert final == "http://recepten.example.com/nieuw" and html == "<html>hier</html>"


async def test_fetch_stops_after_five_redirects():
    async with respx.mock() as mock, httpx.AsyncClient() as http:
        mock.get(f"http://{PUBLIC}/r").respond(302, headers={"Location": "/r"})
        with pytest.raises(RecipeImportError) as e:
            await fetch_page(http, "http://recepten.example.com/r")
        assert len(mock.calls) == 6
    assert e.value.message == "Deze site stuurt te vaak door"


async def test_fetch_stops_at_the_size_cap():
    async with respx.mock() as mock, httpx.AsyncClient() as http:
        mock.get(f"http://{PUBLIC}/groot").respond(200, content=b"x" * 5000)
        with pytest.raises(RecipeImportError) as e:
            await fetch_page(http, "http://recepten.example.com/groot", max_bytes=1000)
        _, html = await fetch_page(http, "http://recepten.example.com/groot",
                                   max_bytes=5000)
    assert e.value.message == "Deze pagina is te groot om te lezen"
    assert len(html) == 5000


@pytest.mark.parametrize("answer,message", [
    (httpx.Response(404), "De site gaf de pagina niet (HTTP 404)"),
    (httpx.ConnectTimeout("t"), "De site reageert niet op tijd"),
    (httpx.ConnectError("c"), "De site is niet bereikbaar"),
])
async def test_fetch_errors_are_plain_dutch(answer, message):
    async with respx.mock() as mock, httpx.AsyncClient() as http:
        mock.get(f"http://{PUBLIC}/x").mock(side_effect=[answer])
        with pytest.raises(RecipeImportError) as e:
            await fetch_page(http, "http://recepten.example.com/x")
    assert e.value.message == message


async def test_unknown_host_is_not_found():
    async with httpx.AsyncClient() as http:
        with pytest.raises(RecipeImportError) as e:
            await fetch_page(http, "https://bestaat-niet.example.com/")
    assert e.value.message == importer.NOT_FOUND


@pytest.mark.parametrize("addr,inward", [
    ("8.8.8.8", False), ("2a00:1450:4001::200e", False),
    ("127.0.0.1", True), ("100.64.0.1", True), ("224.0.0.1", True), ("240.0.0.1", True),
    ("fe80::1%eth0", True), ("fc00::1", True), ("::", True),
    ("::ffff:10.0.0.1", True), ("64:ff9b::a00:1", True), ("2002:c0a8:0101::1", True),
])
def test_is_inward(addr, inward):
    assert importer.is_inward(addr) is inward


async def test_fetch_has_one_deadline_for_everything(monkeypatch):
    monkeypatch.setattr(importer, "DEADLINE", 0.05)

    async def slow(request):
        await asyncio.sleep(1)
        return httpx.Response(200, text="te laat")

    # respx records a call when its answer is ready; this one never is
    async with respx.mock(assert_all_called=False) as mock, httpx.AsyncClient() as http:
        mock.get(f"http://{PUBLIC}/traag").mock(side_effect=slow)
        with pytest.raises(RecipeImportError) as e:
            await fetch_page(http, "http://recepten.example.com/traag")
    assert e.value.message == importer.TIMEOUT
