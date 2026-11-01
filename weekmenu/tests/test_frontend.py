"""The Weekmenu app in a real (headless) Chrome, against web.py with a fake context.

Every screen is rendered on a phone-sized window; the DOM is checked and a screenshot
is saved in tests/screens/ to compare with the mockups by eye. Skipped without Chrome
(the add-on build in Home Assistant has none).
"""

import asyncio
import shutil
import subprocess
import threading
from datetime import date, datetime
from pathlib import Path

import pytest
from aiohttp import web

from app.ahclient import Product, Recipe
from app.main import PERIOD_WARNING
from app.hass import HELPER_OUDERS, _save_total, helper_persons
from app.ledger import WeekLog
from app.planner import Dish, Line, Plan
from app.staples import Staple
from app.tokens import Tokens
from app.web import STATIC_DIR, make_app
from tests.fakes import W, make_ctx

CHROME_PATHS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
]
CHROME = next((p for p in CHROME_PATHS if Path(p).is_file()), None) or shutil.which("chrome")
SCREENS = Path(__file__).parent / "screens"
PHONE = (390, 844)
PHONE_LONG = (390, 1420)  # the whole Week tab, down to "Ook in de bonus"

# Chrome on Windows will not make a window narrower than 500 px, so a 390 px window
# still lays the page out at 500 px. Screenshots of the phone screens therefore show
# the app in a 390 x 844 frame; the DOM checks load the page itself.
FRAME = """<!doctype html><html><head><meta charset="utf-8"><style>
html,body{margin:0;background:#f4f1ec}iframe{display:block;width:390px;height:{h}px;border:0}
</style></head><body><iframe src="../{route}"></iframe></body></html>"""

pytestmark = pytest.mark.skipif(CHROME is None, reason="geen Chrome op deze machine")

# Wednesday of the menu week, late afternoon: the week has started and is counted.
NOW = datetime(2026, 11, 4, 17, 0)
TITLES = [
    "Kip teriyaki met wokgroenten",
    "Pasta met gehaktballetjes in tomatensaus",
    "Pulled-porktaco's met limoenslaw",
    "Hachee met rode kool en puree",
    "Zalm uit de oven met krieltjes",
]


def _p(pid, title, mech=None, regular=3.0, now=None):
    return Product(pid, title, None, regular, now if now is not None else regular,
                   mech is not None, mech, True, None)


def _recipe(rid, title, cook, oven=None, wait=None, steps=()):
    return Recipe(rid, title, f"https://example.invalid/allerhande/{rid}", cook, oven, wait,
                  ["hoofdgerecht"], [], 4, [], list(steps), None)


def _dish(day, rid, title, cuisine, cook, savings, lines, *, oven=None, wait=None,
          persons=2, pantry=(), not_found=(), fish=False, steps=()):
    return Dish(day, _recipe(rid, title, cook, oven, wait, steps), cuisine, fish,
                [Line(p, q, ing) for p, q, ing in lines], savings, persons=persons,
                cook=cook, oven=oven, wait=wait, pantry=list(pantry),
                not_found=list(not_found))


def fake_plan() -> Plan:
    wo, do, za, zo, ma = (date(2026, 11, d) for d in (4, 5, 7, 8, 9))
    kip = _p(1, "AH Kipdijfilet", "2e halve prijs")
    wok = _p(2, "AH Wokgroente oosters", "25% korting")
    teri = _p(3, "Kikkoman teriyakisaus", "1+1 gratis")
    rijst = _p(4, "AH Basmatirijst")
    ui = _p(5, "AH Lente-ui")
    gehakt = _p(6, "AH Gehaktballetjes", "2e halve prijs")
    pasta = _p(7, "De Cecco Penne rigate", "1+1 gratis")
    tomaat = _p(8, "Mutti Passata", "2 voor 3,00")
    kaas = _p(9, "AH Geraspte Parmezaan")
    pork = _p(10, "AH Pulled pork", "30% korting")
    wraps = _p(11, "AH Tortilla wraps")
    kool = _p(12, "AH Rode kool")
    limoen = _p(13, "AH Limoenen")
    hachee = _p(14, "AH Runderlappen", "25% korting")
    puree = _p(15, "AH Aardappelpuree")
    ui2 = _p(16, "AH Uien")
    zalm = _p(17, "AH Zalmfilet", "2e halve prijs")
    kriel = _p(18, "AH Krieltjes")
    bonen = _p(19, "AH Sperziebonen", "1+1 gratis")
    dishes = [
        _dish(wo, 1001, TITLES[0], "Aziatisch", 25, 3.40, [
            (kip, 1, "400 g kipdijfilet"), (wok, 1, "1 zak wokgroente"),
            (teri, 1, "1 fles teriyakisaus"), (rijst, 1, "250 g basmatirijst"),
            (ui, 1, "1 bos lente-ui")],
            pantry=["2 el zonnebloemolie"], steps=[
                "Kook de rijst volgens de aanwijzingen op de verpakking.",
                "Snijd de kipdijfilet in reepjes en bak 6 min in de wok.",
                "Voeg de wokgroente toe en roerbak 4 min.",
                "Schenk de teriyakisaus erbij en verwarm 1 min. Bestrooi met lente-ui."]),
        _dish(do, 1002, TITLES[1], "pasta en Italiaans", 35, 4.10, [
            (gehakt, 1, "300 g gehaktballetjes"), (pasta, 1, "300 g penne"),
            (tomaat, 2, "700 g passata"), (kaas, 1, "40 g Parmezaanse kaas")],
            pantry=["1 el olijfolie", "peper en zout"], not_found=["snufje nootmuskaat"]),
        _dish(za, 1003, TITLES[2], "Mexicaans en wraps", 40, 2.95, [
            (pork, 1, "500 g pulled pork"), (wraps, 1, "8 tortillawraps"),
            (kool, 1, "200 g rode kool"), (limoen, 1, "1 limoen")],
            oven=120, persons=4),
        _dish(zo, 1004, TITLES[3], "Hollandse pot", 30, 2.60, [
            (hachee, 1, "500 g runderlappen"), (kool, 1, "400 g rode kool"),
            (puree, 1, "1 zak aardappelpuree"), (ui2, 1, "3 uien")],
            wait=150, pantry=["2 el bloem", "1 el azijn"]),
        _dish(ma, 1005, TITLES[4], "Hollandse pot", 20, 1.80, [
            (zalm, 1, "2 zalmfilets"), (kriel, 1, "500 g krieltjes"),
            (bonen, 1, "300 g sperziebonen")], fish=True),
    ]
    # links that are not https never reach the page
    dishes[-1].recipe.url = "javascript:alert(1)"
    dishes[-1].recipe.image = "http://example.invalid/zalm.jpg"
    basket: dict[int, int] = {}
    products: dict[int, Product] = {}
    for d in dishes:
        for ln in d.lines:
            basket[ln.product.id] = basket.get(ln.product.id, 0) + ln.qty
            products[ln.product.id] = ln.product
    return Plan(dishes, basket, products, round(sum(d.savings for d in dishes), 2),
                ["snufje nootmuskaat"], 0, pantry=["olie", "zout", "peper", "bloem", "azijn"])


def fake_staples() -> list[Staple]:
    boter = Product(21, "Lurpak Smeerbaar ongezouten", None, 2.95, 2.95, True,
                    "2e Halve Prijs", True, "Zuivel, eieren")
    chips = Product(22, "Lay's Naturel chips", None, 2.49, 2.49, True, "1 + 1 gratis",
                    True, "Borrel, chips, snacks")
    ranja = Product(23, "Karvan Cévitam aardbei siroop", None, 4.19, 2.99, True, "VOOR 2.99",
                    True, "Frisdrank, sappen, water")
    return [Staple(chips, 3, 1, 2, 2.49), Staple(boter, 4, 2, 2, 1.48),
            Staple(ranja, 2, 1, 1, 1.20)]


def build_ctx(tmp_path):
    ctx = make_ctx(tmp_path, now=NOW)
    ctx.store.save(Tokens("test-access", "test-refresh", 9e9))
    ctx.hass.states[HELPER_OUDERS] = "vrijdag"
    ctx.hass.states[helper_persons()["zaterdag"]] = "4"
    plan = fake_plan()
    ctx.ledger.save(WeekLog(W, plan=plan.to_dict(), added=dict(plan.basket), target="order",
                            savings=plan.savings, counted=True,
                            staples=[s.to_dict() for s in fake_staples()]))
    history = [(date(2026, 10, 7), 9.10), (date(2026, 10, 14), 12.40),
               (date(2026, 10, 21), 11.05), (date(2026, 10, 28), 9.75)]
    for week, saved in history:
        old = Plan([], {}, {}, saved, [], 0)
        ctx.ledger.save(WeekLog(week, plan=old.to_dict(), target="order", savings=saved,
                                counted=True))
    _save_total(ctx.total_path, 57.15,
                {**{w.isoformat(): saved for w, saved in history}, W.isoformat(): plan.savings})
    return ctx


class Server:
    """web.py on a free local port in a thread of its own."""

    def __init__(self, ctx, static_dir):
        self.ctx = ctx
        self.static_dir = static_dir
        self.loop = asyncio.new_event_loop()
        self.ready = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        asyncio.set_event_loop(self.loop)
        self.runner = web.AppRunner(make_app(self.ctx, static_dir=self.static_dir))
        self.loop.run_until_complete(self.runner.setup())
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        self.loop.run_until_complete(site.start())
        self.port = site._server.sockets[0].getsockname()[1]
        self.ready.set()
        self.loop.run_forever()

    def __enter__(self):
        self.thread.start()
        assert self.ready.wait(10)
        return f"http://127.0.0.1:{self.port}/"

    def __exit__(self, *exc):
        asyncio.run_coroutine_threadsafe(self.runner.cleanup(), self.loop).result(10)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(10)


def chrome(url, profile, size=(390, 844), screenshot=None) -> str:
    args = [CHROME, "--headless=new", "--disable-gpu", "--hide-scrollbars",
            "--force-device-scale-factor=2", f"--window-size={size[0]},{size[1]}",
            f"--user-data-dir={profile}", "--no-first-run", "--no-default-browser-check",
            "--virtual-time-budget=3000"]
    if screenshot:
        args.append("--screenshot=" + str(screenshot).replace("\\", "/"))
    else:
        args.append("--dump-dom")
    args.append(url)
    out = subprocess.run(args, capture_output=True, timeout=60)
    return out.stdout.decode("utf-8", "replace")


def render(base, route, tmp_path, name, size=PHONE) -> str:
    """The DOM of base+route, and a screenshot (phone: through a 390 px frame)."""
    SCREENS.mkdir(exist_ok=True)
    shot = SCREENS / f"{name}.png"
    shot.unlink(missing_ok=True)
    dom = chrome(base + route, tmp_path / f"profile-{name}-dom", size)
    if size[0] == PHONE[0]:
        frame = f"frame-{name}.html"
        (tmp_path / "static" / frame).write_text(
            FRAME.replace("{route}", route).replace("{h}", str(size[1])), encoding="utf-8")
        target = f"{base}static/{frame}"
    else:
        target = base + route
    chrome(target, tmp_path / f"profile-{name}-shot", size, screenshot=shot)
    assert shot.is_file() and shot.stat().st_size > 10_000, f"geen screenshot van {name}"
    return dom


@pytest.mark.slow
def test_app_screens_in_chrome(tmp_path):
    ctx = build_ctx(tmp_path)
    static = tmp_path / "static"
    shutil.copytree(STATIC_DIR, static)
    with Server(ctx, static) as base:
        dom = render(base, "", tmp_path, "week", size=PHONE_LONG)
        for title in TITLES:
            assert title.replace("'", "&#39;") in dom or title in dom, title
        assert "bij de ouders" in dom
        assert "€ 14,85" in dom
        assert "geschat" in dom
        assert "19 producten" in dom and "10 in de bonus" in dom
        # Ook in de bonus: under the dishes, escaped, with a plus button each
        assert dom.index("Ook in de bonus") > dom.index(TITLES[4])
        assert "Naturel chips" in dom and "Lurpak Smeerbaar ongezouten" in dom
        assert "€ 2,49 bespaard" in dom and "kocht je 3× in 2 mnd" in dom
        assert "1 + 1 gratis" in dom and "2e Halve Prijs" in dom
        assert dom.count('data-staple="') == 3 and "Toegevoegd" not in dom

        dom = render(base, "#gerecht/2026-11-04", tmp_path, "gerecht")
        assert TITLES[0] in dom
        assert "in huis" in dom and "2e halve prijs" in dom and "€ 3,40" in dom
        assert "Recept op Allerhande" in dom  # https link: shown
        assert "Bereiding" in dom and "Ander gerecht" in dom

        dom = render(base, "#gerecht/2026-11-05", tmp_path, "gerecht-niet-gevonden")
        assert "niet gevonden" in dom and "snufje nootmuskaat" in dom

        dom = chrome(base + "#gerecht/2026-11-09", tmp_path / "profile-unsafe")
        assert TITLES[4] in dom
        assert "Recept op Allerhande" not in dom and "javascript:" not in dom
        assert "<img" not in dom and "http://example.invalid" not in dom

        dom = render(base, "#besparing", tmp_path, "besparing")
        assert "€ 57,15" in dom and "€ 14,85" in dom and "wk 45" in dom

        dom = render(base, "#instellingen", tmp_path, "instellingen")
        assert "Kookavonden" in dom and "Bij de ouders" in dom and "Nieuw menu" in dom
        assert "Nu maken" in dom
        assert 'href="#inloggen"' in dom and "Opnieuw inloggen" in dom

        dom = render(base, "#inloggen", tmp_path, "opnieuw-inloggen")
        assert "appie://login-exit" in dom and "Log opnieuw in" in dom
        assert 'href="#instellingen"' in dom  # a way back without logging in

        render(base, "", tmp_path, "week-desktop", size=(1280, 800))

        log = ctx.ledger.load(W)
        log.added[1] += 1  # one pack more than the menu needs now: a leftover
        log.pending = {6: {"before": 0, "delta": 1, "target": "order", "order_id": None}}
        log.unknown = {7: 1}
        log.plan["budget_exhausted"] = True
        log.period_warning = PERIOD_WARNING
        # history failed this week: only the staple already added stays, with the reason
        added = fake_staples()[1]
        added.product.title = "Lurpak <i>ongezouten</i>"  # titles are escaped
        log.staples = [added.to_dict()]
        log.extras_added = {21: 2}
        log.extras_savings = 1.48
        log.staples_error = "Vaste boodschappen niet op te halen (HTTP 500)"
        ctx.ledger.save(log)
        dom = render(base, "", tmp_path, "week-meldingen")
        assert "Geen vaste boodschappen deze week" in dom and "HTTP 500" in dom
        assert 'aria-label="Toegevoegd"' in dom and 'data-staple="' not in dom
        assert "Lurpak &lt;i&gt;ongezouten&lt;/i&gt;" in dom and "<i>ongezouten" not in dom
        assert "waarvan vaste boodschappen € 1,48" in dom and "€ 16,33" in dom
        assert "Over van het vorige gerecht" in dom and "1× AH Kipdijfilet" in dom
        assert "Onzeker of gelukt" in dom and "AH Gehaktballetjes" in dom
        assert "Status onbekend" in dom and "De Cecco Penne rigate" in dom
        assert "Snel gekozen" in dom
        assert "De folder van volgende week was er nog niet" in dom

        ctx.login_expired = True
        dom = render(base, "", tmp_path, "login")
        assert "appie://login-exit" in dom and "F12" in dom
