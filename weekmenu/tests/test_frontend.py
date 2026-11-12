"""The Weekmenu app in a real (headless) Chrome, against web.py with a fake context.

Every screen is rendered on a phone-sized window; the DOM is checked and a screenshot
is saved in tests/screens/ to compare with the mockups by eye. Skipped without Chrome
(the add-on build in Home Assistant has none).
"""

import asyncio
import shutil
import subprocess
import sys
import threading
from datetime import date, datetime
from pathlib import Path

import pytest
from aiohttp import web

from app.ahclient import Product, Recipe
from app.main import PERIOD_WARNING, Waiting
from app.hass import _save_total, helper_persons
from app.ledger import WeekLog
from app.planner import Dish, Line, Plan
from app.staples import Staple
from app.tokens import Tokens
from app.web import STATIC_DIR, make_app
from tests.chrome import find_chrome
from tests.fakes import W, make_ctx

CHROME = find_chrome()
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
    # 6 eat days (dinsdag niet thuis), 5 cooking moments: donderdag is the leftover
    # night of woensdag, which cooks for both days
    wo, do, vr, za, zo, ma = (date(2026, 11, d) for d in (4, 5, 6, 7, 8, 9))
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
                "Schenk de teriyakisaus erbij en verwarm 1 min. Bestrooi met lente-ui."],
            persons=4),
        _dish(vr, 1002, TITLES[1], "pasta en Italiaans", 35, 4.10, [
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
    dishes[0].leftover_date = do
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
    ctx.hass.states[helper_persons()["zaterdag"]] = "4"
    ctx.hass.states[helper_persons()["dinsdag"]] = "0"  # niet thuis
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
    if sys.platform.startswith("linux"):
        args.append("--no-sandbox")  # CI runs the tests inside a container-like runner
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
        # the order line, right under the title and above the savings
        order = "Voor de bestelling van woensdag 4 november"
        assert dom.index("<h1>Weekmenu</h1>") < dom.index(order) < dom.index("Bonus bespaard")
        # its own full-width row, after the "in bestelling" pill rather than beside it
        assert dom.index("in bestelling") < dom.index(order)
        for title in TITLES:
            assert title.replace("'", "&#39;") in dom or title in dom, title
        assert "ouders" not in dom and "Vrijdag</b> · niet thuis" not in dom
        # the leftover night: a quiet card that opens the dish it comes from
        assert ('<button class="card skip rest" data-dish="2026-11-04">' in dom
                and f"Donderdag</b> · restjes van {TITLES[0]}" in dom)
        assert "Dinsdag</b> · niet thuis" in dom
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
        assert "Gerecht weghalen" in dom and "data-remove" in dom
        assert "Ook voor de restjes op donderdag 5 november" in dom and "<b>4</b>" in dom

        dom = render(base, "#gerecht/2026-11-06", tmp_path, "gerecht-niet-gevonden")
        assert "niet gevonden" in dom and "snufje nootmuskaat" in dom

        dom = chrome(base + "#gerecht/2026-11-09", tmp_path / "profile-unsafe")
        assert TITLES[4] in dom
        assert "Recept op Allerhande" not in dom and "javascript:" not in dom
        assert "<img" not in dom and "http://example.invalid" not in dom

        dom = render(base, "#besparing", tmp_path, "besparing")
        assert "€ 57,15" in dom and "€ 14,85" in dom and "wk 45" in dom

        dom = render(base, "#instellingen", tmp_path, "instellingen")
        assert "Kookmomenten" in dom and "Kookavonden" not in dom and "Niet thuis eten" in dom and "ouders" not in dom
        assert "Nieuw menu" not in dom and "Nu maken" not in dom  # v1.1: chosen in the app
        assert 'class="box away"' in dom and "Personen op dinsdag: niet thuis" in dom
        assert "6 eetdagen, 5 kookmomenten, 1 keer restjes" in dom
        assert 'aria-label="Meer kookmomenten" disabled' not in dom  # 5 of 6: + still works
        # chips wo..di: only di is pressed (at 0 in Home Assistant)
        assert dom.count('class="chip x"') == 1
        assert 'class="chip x" data-away="dinsdag"' in dom
        assert 'class="chip" data-away="woensdag"' in dom and 'class="chip" data-away="vrijdag"' in dom
        assert dom.count("disabled") == 1 and "<b>–</b>" in dom  # only the dinsdag box
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
        # "Gerecht weghalen" on maandag: the evening stays on the week as "geen gerecht"
        log.plan["dishes"] = [x for x in log.plan["dishes"] if x["date"] != "2026-11-09"]
        log.plan["removed_days"] = ["2026-11-09"]
        # zaterdag: a cook day the planner could not fill is "geen kookmoment"
        log.plan["dishes"] = [x for x in log.plan["dishes"] if x["date"] != "2026-11-07"]
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
        assert "Over van het vorige menu" in dom and "1× AH Kipdijfilet" in dom
        assert "Nog niet zeker of dit in je bestelling staat" in dom
        assert "Onzeker" not in dom and "AH Gehaktballetjes" in dom
        assert "Status onbekend" in dom and "De Cecco Penne rigate" in dom
        assert "Snel gekozen" in dom
        assert "Maandag</b> · geen gerecht" in dom and TITLES[4] not in dom
        assert "Zaterdag</b> · geen kookmoment" in dom and "Maandag</b> · geen kookmoment" not in dom
        assert "De folder van volgende week was er nog niet" in dom

        # a dish with leftovers goes up to 16 persons: + still works at 9, − too
        for x in log.plan["dishes"]:
            if x["date"] == "2026-11-04":
                x["persons"] = 9
        ctx.ledger.save(log)
        dom = chrome(base + "#gerecht/2026-11-04", tmp_path / "profile-sixteen")
        assert "<b>9</b>" in dom and dom.count("disabled") == 0

        ctx.login_expired = True
        dom = render(base, "", tmp_path, "login")
        assert "appie://login-exit" in dom and "F12" in dom


@pytest.mark.slow
def test_app_waiting_for_folder_in_chrome(tmp_path):
    """Thursday before the menu week of 11 November: no folder yet, no menu."""
    ctx = make_ctx(tmp_path, now=datetime(2026, 11, 5, 10, 0))
    ctx.store.save(Tokens("test-access", "test-refresh", 9e9))
    ctx.waiting = Waiting(date(2026, 11, 11), date(2026, 11, 6))
    static = tmp_path / "static"
    shutil.copytree(STATIC_DIR, static)
    with Server(ctx, static) as base:
        dom = render(base, "", tmp_path, "week-wachten")
        ctx.hass.states["input_boolean.weekmenu_aan"] = "off"
        off = chrome(base, tmp_path / "profile-wachten-uit")
    line = "Volgende bestelling: woensdag 11 november. De folder komt vanaf vrijdag 6 november."
    assert dom.index("<h1>Weekmenu</h1>") < dom.index(line)
    # v1.1: no Nu maken any more; the week is chosen evening by evening
    assert "De folder van volgende week is er nog niet" not in dom
    assert 'data-run="nu_maken"' not in dom and "Nu maken" not in dom
    assert "Nog geen menu voor deze week" in dom and "Kies per avond een gerecht" in dom
    assert "Voor de bestelling van" not in dom and "\u2014" not in dom
    assert "Het weekmenu staat uit" in off and "vanzelf" not in off


@pytest.mark.slow
def test_app_new_folder_card_in_chrome(tmp_path):
    """Friday 30 October: the menu was made on Thursday with the old folder, the new one
    is out. On top of the Week tab: "Nieuwe folder is er" with Menu bijwerken."""
    ctx = make_ctx(tmp_path, now=datetime(2026, 10, 30, 10, 0))
    ctx.store.save(Tokens("test-access", "test-refresh", 9e9))
    plan = fake_plan()
    ctx.ledger.save(WeekLog(W, plan=plan.to_dict(), added=dict(plan.basket), target="order",
                            savings=plan.savings, period_warning=PERIOD_WARNING,
                            folder_period_start=date(2026, 10, 26),
                            new_folder=date(2026, 11, 2), new_folder_notified=True))
    static = tmp_path / "static"
    shutil.copytree(STATIC_DIR, static)
    with Server(ctx, static) as base:
        dom = render(base, "", tmp_path, "week-nieuwe-folder")
        log = ctx.ledger.load(W)
        log.folder_period_start = date(2026, 11, 2)  # after Menu bijwerken
        log.period_warning = None
        ctx.ledger.save(log)
        after = chrome(base, tmp_path / "profile-nieuwe-folder-na")
    order = "Voor de bestelling van woensdag 4 november"
    assert dom.index(order) < dom.index("Nieuwe folder is er") < dom.index("Bonus bespaard")
    assert 'data-run="bijwerken"' in dom and "Menu bijwerken" in dom
    assert "\u2014" not in dom
    assert "Nieuwe folder is er" not in after and 'data-run="bijwerken"' not in after
    assert order in after


def fake_offers() -> list[Product]:
    def o(pid, title, cat, mech, regular, now=None, image=None, unit="500 g"):
        return Product(pid, title, unit, regular, regular if now is None else now, True, mech,
                       True, cat, image=image)

    img = "https://example.invalid/p.jpg"  # no network: the photo block falls back
    return [
        o(61, "AH Roomboter ongezouten", "Zuivel, eieren", "2e halve prijs", 2.95, image=img),
        o(62, "AH Halfvolle melk", "Zuivel, eieren", "25% korting", 1.35, 1.01, image=img,
          unit="1 l"),
        o(63, "Lay's Naturel chips", "Borrel, chips, snacks", "1 + 1 gratis", 2.49, image=img),
        o(64, "Zeep <b>fris</b>", "Drogisterij", "2 voor 5.00", 3.19,
          image="http://example.invalid/zeep.jpg"),  # not https: never shown
        o(65, "Karvan Cévitam aardbei siroop", "Frisdrank, sappen, water", "VOOR 2.99", 4.19,
          2.99, image=img),
    ]


@pytest.mark.slow
def test_app_offers_tab_in_chrome(tmp_path):
    ctx = build_ctx(tmp_path)
    ctx.bonus_cache = {date(2026, 11, 2): fake_offers()}  # the folder of the menu week
    # one offer was added: a check with the quantity, and its own line on the Week tab
    log = ctx.ledger.load(W)
    log.offers_added = {63: 2}
    log.offers_savings = 2.49
    log.extras_added, log.extras_savings = {21: 2}, 1.48
    ctx.ledger.save(log)
    static = tmp_path / "static"
    shutil.copytree(STATIC_DIR, static)
    with Server(ctx, static) as base:
        dom = render(base, "#aanbiedingen", tmp_path, "aanbiedingen")
        # four items in the bottom bar, Aanbiedingen between Week and Besparing
        assert dom.index('data-tab="week"') < dom.index('data-tab="aanbiedingen"') < \
            dom.index('data-tab="besparing"') < dom.index('data-tab="instellingen"')
        assert dom.count('data-tab="') == 4
        # the search field on top, then a heading per category
        assert dom.index('id="offerq"') < dom.index('<div class="sec">Zuivel, eieren')
        for cat in ("Borrel, chips, snacks", "Drogisterij", "Frisdrank, sappen, water",
                    "Zuivel, eieren"):
            assert f'<div class="sec">{cat}</div>' in dom, cat
        # in the folder's own order (AH's "Alle Bonus"), not A to Z
        assert dom.index(">Zuivel, eieren<") < dom.index(">Borrel, chips, snacks<") <             dom.index(">Drogisterij<") < dom.index(">Frisdrank, sappen, water<")
        assert "5 aanbiedingen" in dom
        # pill with the action, price, the old price struck through only when it is lower
        assert "2e halve prijs" in dom and "1 + 1 gratis" in dom
        assert "<s>€ 1,35</s>" in dom and "€ 1,01" in dom and "<s>€ 2,49</s>" not in dom
        assert dom.count('data-offer="') == 4 and 'aria-label="Toegevoegd: 2 stuks"' in dom
        # escaped titles, https photos only
        assert "Zeep &lt;b&gt;fris&lt;/b&gt;" in dom and "<b>fris" not in dom
        assert "http://example.invalid" not in dom  # (https photos fail to load offline and remove themselves)

        week = chrome(base, tmp_path / "profile-offers-week")
        assert "waarvan losse aanbiedingen € 2,49" in week and "€ 18,82" in week
        assert "waarvan vaste boodschappen € 1,48" in week
        assert "—" not in dom


@pytest.mark.slow
def test_app_diagnose_paused_and_update_in_chrome(tmp_path):
    """v0.7: Diagnose in Instellingen, the paused note on the week, and the update
    screen instead of the login when tokens.json is from a newer add-on."""
    from app.health import PAUSED_NOTE

    ctx = build_ctx(tmp_path)
    static = tmp_path / "static"
    shutil.copytree(STATIC_DIR, static)
    with Server(ctx, static) as base:
        dom = render(base, "#instellingen", tmp_path, "instellingen-diagnose")
        assert "Diagnose" in dom and "AH antwoordt zoals verwacht" in dom
        assert "Kopieer diagnose" in dom and "data-diagretry" not in dom
        assert "Uitloggen bij AH" in dom and "AH-appversie 9.28" in dom

        ctx.health.failed("lijst", "vorm")
        ctx.health.failed("lijst", "vorm")
        dom = render(base, "#instellingen", tmp_path, "instellingen-stil")
        assert "Schrijven staat stil" in dom and "Opnieuw proberen" in dom
        dom = chrome(base, tmp_path / "profile-week-stil")
        assert PAUSED_NOTE in dom

        ctx.store.path.write_text('{"schema": 99}', encoding="utf-8")
        dom = render(base, "", tmp_path, "werk-bij")
        assert "Werk de add-on bij" in dom and "appie://login-exit" not in dom


@pytest.mark.slow
def test_app_v08_tonight_login_and_welcome_in_chrome(tmp_path):
    """v0.8: Vanavond on top of the week, the login line, the red login card, the
    welcome screen on a first start, the step plan to log in, and the guide."""
    import os

    ctx = build_ctx(tmp_path)
    static = tmp_path / "static"
    shutil.copytree(STATIC_DIR, static)
    with Server(ctx, static) as base:
        dom = chrome(base, tmp_path / "profile-v08-week")
        # NOW is woensdag 4 november: the teriyaki, 25 min before 18:30, at 18:00
        assert f"<b>Vanavond</b><span>{TITLES[0]}, begin om 18:00</span>" in dom
        assert dom.index("<b>Vanavond</b>") < dom.index("Bonus bespaard")
        assert "AH-login in orde, laatst ververst" in dom and "login-bad" not in dom

        # Ruling 50: an old refresh is shown, never judged
        old = datetime(2026, 10, 20, 9, 30).timestamp()
        os.utime(ctx.store.path, (old, old))
        dom = chrome(base, tmp_path / "profile-v08-old")
        assert "AH-login in orde, laatst ververst 20 oktober 09:30" in dom
        assert "login-bad" not in dom and "7 dagen" not in dom

        dom = chrome(base + "gids", tmp_path / "profile-gids")
        assert "Weekmenu: de gids" in dom and 'href="./"' in dom
        dom = chrome(base + "#instellingen", tmp_path / "profile-v08-set")
        assert 'href="gids"' in dom

    fresh = make_ctx(tmp_path / "fresh", now=NOW)
    with Server(fresh, static) as base:
        dom = render(base, "", tmp_path, "welkom")
        assert "Welkom bij Weekmenu" in dom and "data-welcome" in dom
        assert dom.count("<li>") == 3 and 'href="gids"' in dom
        assert "appie://login-exit" not in dom and "—" not in dom
        dom = render(base, "#inloggen", tmp_path, "login-stappen")
        assert "Op een computer:" in dom and "Druk F12 en kies Console." in dom
        assert "via WhatsApp naar jezelf" in dom and "<summary>Hoe werkt dit?</summary>" in dom


@pytest.mark.slow
def test_app_v09_check_card_klopt_niet_and_corrections_in_chrome(tmp_path):
    """v0.9: "Check voor je bestelt" under the order line (an action per product not
    found, "Alles staat klaar" when nothing is left), "Klopt niet" on every ingredient
    line and its screen with three choices, and Mijn correcties in Instellingen."""
    from app.ingredients import PRODUCT, SKIP
    from app.overrides import make_entry

    ctx = build_ctx(tmp_path)
    log = ctx.ledger.load(W)
    for x in log.plan["dishes"]:
        if x["date"] == "2026-11-06":
            x["lines"][0]["check"] = True  # gehaktballetjes per 100 gram: 1 piece
    ctx.ledger.save(log)
    static = tmp_path / "static"
    shutil.copytree(STATIC_DIR, static)
    with Server(ctx, static) as base:
        dom = render(base, "", tmp_path, "week-check", size=PHONE_LONG)
        order = "Voor de bestelling van woensdag 4 november"
        assert dom.index(order) < dom.index("Check voor je bestelt") < dom.index("Bonus bespaard")
        assert "1 niet gevonden · 1 hoeveelheid controleren" in dom
        assert 'data-fixday="2026-11-06" data-fix="snufje nootmuskaat"' in dom
        assert "Zoek product" in dom and 'data-skip="snufje nootmuskaat"' in dom
        assert "Hoeveelheid controleren" in dom and "AH Gehaktballetjes (300 g gehaktballetjes)" in dom
        assert "Alles staat klaar" not in dom and "Goed om te weten" not in dom

        dom = render(base, "#gerecht/2026-11-06", tmp_path, "gerecht-klopt-niet")
        # every line: 4 products, 2 pantry, 1 not found
        assert dom.count('class="fixbtn"') == 7 and "hoeveelheid controleren" in dom

        dom = render(base, "#klopt/2026-11-06/snufje%20nootmuskaat", tmp_path, "klopt-niet")
        assert "<h1>Klopt niet</h1>" in dom and "Nu: Niet gevonden bij AH" in dom
        assert 'id="fixq"' in dom and 'value="snufje nootmuskaat"' in dom
        assert "Hoeft niet" in dom and "Hebben we al" in dom and "Mijn correcties" in dom
        assert 'id="nav" class="nav" hidden' in dom  # no bottom bar here
        dom = chrome(base + "#klopt/2026-11-06/400%20g%20onbekend", tmp_path / "profile-gone")
        assert "staat niet meer in het gerecht" in dom

        log = ctx.ledger.load(W)
        for x in log.plan["dishes"]:
            x["not_found"] = []
            for ln in x["lines"]:
                ln["check"] = False
        log.plan["not_found"] = []
        ctx.ledger.save(log)
        dom = render(base, "", tmp_path, "week-alles-klaar")
        assert "Alles staat klaar" in dom and "19 producten in je bestelling" in dom
        assert "Check voor je bestelt" not in dom

        # 1.0 final review I1: a product the menu needs but never added is never green
        log = ctx.ledger.load(W)
        gone = next(iter(log.added))
        had = log.added.pop(gone)
        ctx.ledger.save(log)
        dom = chrome(base, tmp_path / "profile-v10-missing")
        assert "Alles staat klaar" not in dom and "1 nog niet toegevoegd" in dom
        assert "<b>Nog niet toegevoegd</b>" in dom and "nog niet in je bestelling gezet" in dom
        log.added[gone] = had
        ctx.ledger.save(log)

        ctx.overrides.put("nootmuskaat", make_entry(SKIP, "snufje nootmuskaat", None, NOW))
        ctx.overrides.put("rode ui", make_entry(
            PRODUCT, "1 rode ui", Product(31, "AH Rode uien", "1 kg", 1.5, 1.5, False, None,
                                          True, None), NOW))
        dom = render(base, "#instellingen", tmp_path, "instellingen-correcties",
                     size=PHONE_LONG)
        assert dom.index("Mijn correcties") < dom.index("Diagnose")
        assert "Ander product: AH Rode uien" in dom and "Hoeft niet" in dom
        assert dom.count('data-unfix="') == 2 and "Terugdraaien" in dom
        assert "nootmuskaat<small>Hoeft niet" in dom  # the key the choice holds for
        assert "—" not in dom


@pytest.mark.slow
def test_app_v10_cooking_screen_taste_and_never_list_in_chrome(tmp_path):
    """v1.0: Koken on the dish screen opens one scrolling cooking screen (amounts big,
    the AH product small under it, numbered steps, Klaar); Duim omlaag and Nooit meer on
    both screens; Nooit meer in Instellingen with Terugdraaien."""
    from app.taste import DOWN, NEVER

    ctx = build_ctx(tmp_path)
    static = tmp_path / "static"
    shutil.copytree(STATIC_DIR, static)
    with Server(ctx, static) as base:
        dom = render(base, "#gerecht/2026-11-04", tmp_path, "gerecht-koken", size=PHONE_LONG)
        assert "data-cook" in dom and "Koken" in dom
        assert 'data-taste="down"' in dom and 'data-taste="never"' in dom
        assert dom.index("Koken") < dom.index("Ingrediënten")

        dom = render(base, "#koken/2026-11-04", tmp_path, "koken", size=PHONE_LONG)
        assert "<b>400 g kipdijfilet</b><small>AH Kipdijfilet</small>" in dom
        assert "<b>2 el zonnebloemolie</b><small>in huis</small>" in dom
        assert dom.count('data-cstep="') == 4 and '<span class="n">1</span>' in dom
        assert dom.index("Ingrediënten") < dom.index("Bereiding")
        assert dom.count("data-cookdone") == 2 and 'id="nav" class="nav" hidden' in dom
        assert "Duim omlaag" in dom and "Nooit meer" in dom

        ctx.taste.put(1001, DOWN, TITLES[0], NOW)
        dom = chrome(base + "#gerecht/2026-11-04", tmp_path / "profile-down")
        assert 'data-taste="down" aria-pressed="true"' in dom
        assert "Komt minder vaak in het menu." in dom

        from app.ingredients import SKIP
        from app.overrides import make_entry
        ctx.overrides.put("snufje nootmuskaat", make_entry(SKIP, "snufje nootmuskaat", None,
                                                           NOW))
        ctx.taste.put(2001, NEVER, "Lasagne met spinazie", NOW)
        ctx.taste.put(2002, NEVER, "Bloemkoolcurry", NOW)
        dom = render(base, "#instellingen", tmp_path, "instellingen-nooit-meer",
                     size=PHONE_LONG)
        assert dom.index("Mijn correcties") < dom.index("Nooit meer") < dom.index("Diagnose")
        assert dom.count('data-unnever="') == 2 and "Lasagne met spinazie" in dom
        assert TITLES[0] not in dom.split("Nooit meer", 1)[1]  # a thumb is not listed
        assert "—" not in dom


@pytest.mark.slow
def test_app_v10_savings_with_recounted_weeks_and_numbers_in_chrome(tmp_path):
    """v1.0: Besparing keeps the estimate (month, total, bars) and adds "Echt bespaard
    (nagerekend)" per delivered week, "niet na te rekenen" when there was no order, and
    the two numbers over the last four weeks."""
    from app.ingredients import SKIP
    from app.overrides import make_entry
    from app.reconcile import OK, UNCHECKABLE

    ctx = build_ctx(tmp_path)
    log = ctx.ledger.load(W)
    for x in log.plan["dishes"]:  # the recipe lines, as AH gives them
        x["recipe"]["ingredients"] = ([[ln["from_ingredient"], ""] for ln in x["lines"]]
                                      + [[t, ""] for t in x["pantry"] + x["not_found"]])
    ctx.ledger.save(log)
    ctx.overrides.put("snufje nootmuskaat", make_entry(SKIP, "snufje nootmuskaat", None, NOW))
    r = ctx.reconciliations
    r.put("2026-10-14", {"status": UNCHECKABLE, "reason": "Geen bezorgde bestelling "
                         "gevonden in de menuweek. Gekocht in de winkel is niet na te rekenen."})
    r.put("2026-10-21", {"status": OK, "bought_share": {"bought": 16, "added": 18},
                         "realised_savings": 9.85})
    r.put("2026-10-28", {"status": OK, "bought_share": {"bought": 17, "added": 19},
                         "realised_savings": 8.40})
    static = tmp_path / "static"
    shutil.copytree(STATIC_DIR, static)
    with Server(ctx, static) as base:
        dom = render(base, "#besparing", tmp_path, "besparing-nagerekend", size=PHONE_LONG)
        assert "€ 57,15" in dom  # the total stays the estimate
        assert dom.count("Echt bespaard (nagerekend)") == 3
        assert "€ 8,40" in dom and "17 van 19 toegevoegde producten echt gekocht" in dom
        assert "niet na te rekenen" in dom and "Gekocht in de winkel" in dom
        assert "Meetgetallen, laatste 4 weken" in dom
        assert "Ingrediëntregels zonder ingreep" in dom and "(25 van 26)" in dom
        assert "Toegevoegd en echt gekocht" in dom and "89%" in dom and "(33 van 37)" in dom
        assert "—" not in dom
