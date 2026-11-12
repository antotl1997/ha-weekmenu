import json
import re
from datetime import date
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

import app.ahclient as ahclient
from app import planner
from app.ahclient import AhApiError, AhClient, Product, Recipe, RecipeSummary
from app.planner import Plan, core_word, cuisine_of, empty_plan, pick_dish, rescale_dish

PANTRY = ["olie", "zout", "peper", "suiker", "bloem", "azijn"]

# Menuweek of woensdag 4 november 2026
WO, DO, VR, ZA, ZO, MA, DI = (date(2026, 11, d) for d in (4, 5, 6, 7, 8, 9, 10))


def bonus(id, title, regular=5.0, now=3.0, mech=None, cat="Vlees", unit="500 g"):
    return Product(id, title, unit, regular, now, True, mech, True, cat)


def R(id, title, ings, cook=30, oven=None, wait=None, cuisines=(), courses=("hoofdgerecht",)):
    """A fake recipe for 2 persons. ings: (qty, unit, name); qty None = no number."""
    return dict(id=id, title=title, ings=ings, cook=cook, oven=oven, wait=wait,
                cuisines=list(cuisines), courses=list(courses))


class FakeClient:
    """Allerhande and product search in memory, counting every call.

    Every recipe search returns every recipe (unless `searches` maps the term).
    Product search finds a plain, non-bonus product for every name except `missing`.
    """

    def __init__(self, recipes, searches=None, missing=()):
        self.recipes = {r["id"]: r for r in recipes}
        self.searches = searches
        self.missing = set(missing)
        self.search_calls = []
        self.recipe_calls = []
        self.product_calls = []
        self.fail_ids = set()
        self.noname = set()  # ingredients that come without a name, like some AH ones
        self._pids = {}

    def add(self, *recipes):
        for r in recipes:
            self.recipes[r["id"]] = r

    async def search_recipes(self, text, size=30):
        self.search_calls.append(text)
        ids = self.searches.get(text, []) if self.searches is not None else list(self.recipes)
        return [
            RecipeSummary(
                id=r["id"], title=r["title"], slug=f"r{r['id']}", courses=r["courses"],
                cook=r["cook"], oven=r["oven"], wait=r["wait"], servings=2,
                image=f"https://example.invalid/{r['id']}.jpg",
            )
            for r in (self.recipes[i] for i in ids if i in self.recipes)
        ]

    async def get_recipe(self, id, servings):
        self.recipe_calls.append((id, servings))
        if id in self.fail_ids:
            raise AhApiError(f"recipe {id} not found", 200)
        r = self.recipes[id]
        f = servings / 2
        ings = []
        for qty, unit, name in r["ings"]:
            if qty is None:
                ings.append((name, "" if name in self.noname else name))
            else:
                text = f"{qty * f:g} {unit} {name}".replace("  ", " ")
                ings.append((text, "" if name in self.noname else name))
        return Recipe(
            id=id, title=r["title"], url=f"https://www.ah.nl/allerhande/recept/R-R{id}/r",
            cook=r["cook"], oven=None, wait=None, courses=r["courses"],
            cuisines=r["cuisines"], servings=servings, ingredients=ings,
            steps=["Kook het."], image=f"https://example.invalid/{id}.jpg",
        )

    async def search_products(self, query, size=20):
        self.product_calls.append(query)
        if query in self.missing:
            return []
        pid = self._pids.setdefault(query, 90000 + len(self._pids))
        return [Product(pid, query.capitalize(), "per stuk", 1.0, 1.0, False, None, True, None)]


GEHAKT = bonus(1, "AH Rundergehakt", regular=6.0, now=3.0)  # saves 3.00 per pack
KIP = bonus(2, "AH Scharrelkipfilet", regular=5.0, now=3.5)  # saves 1.50
ZALM = bonus(3, "AH Zalmfilet", regular=8.0, now=5.0, cat="Vis")  # saves 3.00
WORST = bonus(4, "AH Rookworst", regular=3.0, now=2.0, unit="275 g")  # saves 1.00


def dish_on(plan, day):
    return next(d for d in plan.dishes if d.date == day)


async def menu(client, bonus_list, picks, persons=None, leftovers=None):
    """The menu of `picks` ({day: recipe id}), made the way the app makes it: one
    picked dish after the other on an empty week (v1.1: the add-on never chooses)."""
    plan = empty_plan(PANTRY)
    for day, rid in sorted(picks.items()):
        plan = await pick_dish(client, plan, day, rid, bonus_list,
                               (persons or {}).get(day, 2), (leftovers or {}).get(day))
    return plan


# --- the basket of a menu ------------------------------------------------------------


async def test_pantry_items_not_in_basket():
    client = FakeClient([
        R(70, "Stamppot met gehakt", [
            (500, "g", "rundergehakt"), (2, "el", "milde olijfolie"), (None, "", "zout"),
            (None, "", "versgemalen peper"),
        ]),
    ])
    plan = await menu(client, [GEHAKT], {WO: 70})
    assert set(plan.basket) == {1}
    assert client.product_calls == []  # pantry is never searched
    assert plan.dishes[0].pantry == ["2 el milde olijfolie", "zout", "versgemalen peper"]


async def test_shared_product_merged_once():
    client = FakeClient([
        R(80, "Stamppot met gehakt", [(500, "g", "rundergehakt"), (1, "stuks", "ui")]),
        R(81, "Curry met kip", [(500, "g", "scharrelkipfilet"), (1, "stuks", "ui")]),
        R(82, "Pasta met zalm", [(400, "g", "zalmfilet"), (1, "stuks", "ui")]),
    ])
    plan = await menu(client, [GEHAKT, KIP, ZALM], {WO: 80, DO: 81, VR: 82})
    assert len(plan.dishes) == 3
    ui = [pid for pid, p in plan.products.items() if p.title == "Ui"]
    assert len(ui) == 1
    assert plan.basket[ui[0]] == 3


async def test_smart_topup_only_when_shared():
    kip11 = bonus(2, "AH Scharrelkipfilet", regular=4.0, now=4.0, mech="1+1 gratis")
    one = FakeClient([
        R(90, "Curry met kip", [(500, "g", "scharrelkipfilet")]),
    ])
    plan = await menu(one, [kip11], {WO: 90})
    assert plan.basket[2] == 1
    assert plan.savings == 0.0  # the deal is not reached, so it saves nothing

    shared = FakeClient([
        R(90, "Curry met kip", [(500, "g", "scharrelkipfilet")]),
        R(91, "Pasta met kip", [(500, "g", "scharrelkipfilet")]),
    ])
    plan = await menu(shared, [kip11], {WO: 90, DO: 91})
    assert plan.basket[2] == 2
    assert plan.savings == 4.0
    assert [d.savings for d in plan.dishes] == [2.0, 2.0]

    three = FakeClient([
        R(90, "Curry met kip", [(500, "g", "scharrelkipfilet")]),
        R(91, "Pasta met kip", [(1000, "g", "scharrelkipfilet")]),
    ])
    plan = await menu(three, [kip11], {WO: 90, DO: 91})
    assert plan.basket[2] == 4  # 3 needed, shared: top up to the next 1+1
    assert plan.savings == 8.0
    by_id = {d.recipe.id: d.savings for d in plan.dishes}
    assert by_id == {90: pytest.approx(2.67, abs=0.01), 91: pytest.approx(5.33, abs=0.01)}

    # Ruling 19: a paid extra ("2e halve prijs") is never bought beyond the merged need
    kip_half = bonus(2, "AH Scharrelkipfilet", regular=4.0, now=4.0, mech="2e halve prijs")
    paid = FakeClient([
        R(90, "Curry met kip", [(500, "g", "scharrelkipfilet")]),
        R(91, "Pasta met kip", [(1000, "g", "scharrelkipfilet")]),
    ])
    plan = await menu(paid, [kip_half], {WO: 90, DO: 91})
    assert plan.basket[2] == 3  # 3 needed: no 4th at half price
    assert plan.savings == 2.0  # one pair reaches the deal


async def test_half_price_second_not_topped_up_when_merged_need_fits_one_pack():
    kip_half = bonus(2, "AH Scharrelkipfilet", regular=4.0, now=4.0, mech="2e halve prijs")
    client = FakeClient([
        R(360, "Curry met kip", [(200, "g", "scharrelkipfilet")]),
        R(361, "Pasta met kip", [(200, "g", "scharrelkipfilet")]),
    ])
    plan = await menu(client, [kip_half], {WO: 360, DO: 361})
    assert len(plan.dishes) == 2
    assert plan.basket[2] == 1
    assert plan.savings == 0.0


async def test_free_unit_deal_tops_up_when_shared_even_if_one_pack_is_enough():
    client = FakeClient([
        R(370, "Curry met kip", [(200, "g", "scharrelkipfilet")]),
        R(371, "Pasta met kip", [(200, "g", "scharrelkipfilet")]),
    ])
    plan = await menu(client, [KIP11], {WO: 370, DO: 371})
    assert plan.basket[2] == 2  # the second one is free
    assert plan.savings == 4.0


async def test_persons_per_evening_scale_recipe():
    client = FakeClient([
        R(110, "Stamppot met gehakt", [(500, "g", "rundergehakt")]),
    ])
    plan = await menu(client, [GEHAKT], {DO: 110}, persons={DO: 4})
    assert (110, 4) in client.recipe_calls
    d = plan.dishes[0]
    assert d.persons == 4
    assert d.lines[0].qty == 2  # 1000 g in packs of 500 g
    assert plan.basket[1] == 2
    assert d.savings == 6.0


async def test_rescale_recomputes_basket_and_savings():
    client = FakeClient([
        R(170, "Stamppot met gehakt", [
            (500, "g", "rundergehakt"), (1, "stuks", "ui"), (2, "el", "olijfolie"),
        ]),
    ])
    plan = await menu(client, [GEHAKT], {WO: 170})
    assert plan.basket[1] == 1 and plan.savings == 3.0
    calls = len(client.product_calls)
    new = await rescale_dish(client, plan, WO, 4)
    assert (170, 4) in client.recipe_calls
    d = new.dishes[0]
    assert d.persons == 4
    assert new.basket[1] == 2
    assert new.savings == 6.0 and d.savings == 6.0
    ui = next(pid for pid, p in new.products.items() if p.title == "Ui")
    assert new.basket[ui] == 2
    assert d.pantry == ["4 el olijfolie"]
    assert len(client.product_calls) == calls  # same products, no new searches
    # the weekday default is untouched: only this dish changed
    assert plan.dishes[0].persons == 2


# --- extra coverage ----------------------------------------------------------------


async def test_ingredient_without_product_goes_to_not_found():
    client = FakeClient(
        [R(210, "Stamppot met gehakt", [(500, "g", "rundergehakt"),
                                        (None, "", "snufje nootmuskaat")])],
        missing={"snufje nootmuskaat"},
    )
    plan = await menu(client, [GEHAKT], {WO: 210})
    assert plan.not_found == ["snufje nootmuskaat"]
    assert plan.dishes[0].not_found == ["snufje nootmuskaat"]
    assert len(plan.dishes) == 1


async def test_plan_round_trips_through_json():
    client = FakeClient([
        R(250, "Stamppot met gehakt", [(500, "g", "rundergehakt"), (2, "el", "olijfolie"),
                                       (None, "", "nootmuskaat")], oven=10),
        R(251, "Pasta met zalm", [(400, "g", "zalmfilet"), (1, "stuks", "ui")]),
    ], missing={"nootmuskaat"})
    plan = await menu(client, [GEHAKT, ZALM], {WO: 250, DO: 251})
    text = json.dumps(plan.to_dict())
    back = Plan.from_dict(json.loads(text))
    assert back == plan
    assert isinstance(back.dishes[0].date, date)
    assert all(isinstance(k, int) for k in back.basket)
    assert isinstance(back.dishes[0].recipe.ingredients[0], tuple)


# --- pure helpers ------------------------------------------------------------------


def rec(title, cuisines=(), names=()):
    return Recipe(1, title, "u", 30, None, None, ["hoofdgerecht"], list(cuisines), 2,
                  [(n, n) for n in names], [], None)


@pytest.mark.parametrize("title,cuisines,names,term,expected", [
    ("Volkoren taco's met kip tinga", ["mexicaans"], [], "", "Mexicaans en wraps"),
    ("Witlofstamppot met visfrietjes", [], [], "", "Hollandse pot"),
    ("Mexicaanse ovenschotel", [], [], "", "Mexicaans en wraps"),
    ("Gnocchi-ovenschotel met spekjes", [], [], "", "pasta en Italiaans"),
    ("Eenpansorzo met pastinaak, kip en peer", [], [], "", "pasta en Italiaans"),
    ("Gele-curry-bloemkoolsteak met rijst", [], [], "", "Aziatisch"),
    ("Rijsttraybake met kip en tempeh", [], ["ketjap manis", "sambal oelek", "tempeh"], "",
     "Aziatisch"),
    ("Linzensoep met venkelworst", [], ["varkensbraadworst"], "", "Hollandse pot"),
    ("Zalm met groente", [], ["zalm"], "stamppot", "Hollandse pot"),
    ("Shakshuka met feta", [], ["feta", "ei"], "gehakt", None),
    ("Stamppot met Chinese kool, worst en appel", [], [], "", "Hollandse pot"),
])
def test_cuisine_of(title, cuisines, names, term, expected):
    assert cuisine_of(rec(title, cuisines, names), term) == expected


@pytest.mark.parametrize("title,expected", [
    ("AH Rundergehakt 300 gram", "gehakt"),
    ("AH Zeebaarsfilet op huid", "zeebaars"),
    ("AH Iets kruimige aardappelen 3 kilo", "aardappel"),
    ("AH Baby avocado eetrijp 4 stuks", "avocado"),
    ("Cucina kipshoarma", "shoarma"),
    ("AH Prei", "prei"),
])
def test_core_word(title, expected):
    assert core_word(title) == expected


@pytest.mark.parametrize("text,expected", [
    ("Witlofstamppot met visfrietjes", True),
    ("Herfst ratatouille met koolvis", True),
    ("Panlasagne met zalm en broccoli", True),
    ("Curry met garnalen", True),
    ("Pad thai met vissaus", False),
    ("Pasta puttanesca met ansjovis", False),
    ("Stamppot met kip", False),
])
def test_is_fishy(text, expected):
    assert planner.is_fishy(text) is expected


# --- the recorded AH answers (Step 5) --------------------------------------------------

FIX = Path(__file__).parent / "fixtures"


def _slug(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:80] or "x"


class FixtureAhClient(AhClient):
    """The real AhClient parsing on top of the recorded answers; nothing goes online.

    Recipe searches only exist for the recorded terms (kip, gehakt, zalm, pasta,
    stamppot, curry, wraps, ovenschotel); a term finds the recording whose term is part
    of it ("rundergehakt" -> gehakt) and nothing otherwise. Only 20 recipe details are
    recorded; any other id answers like AH does for an unknown recipe.
    """

    PERIOD = "p1"

    def __init__(self):
        super().__init__(store=None, http=None)
        self.calls = []

    def _load(self, name):
        f = FIX / name
        return json.loads(f.read_text(encoding="utf-8")) if f.exists() else None

    async def request(self, method, path, *, json=None, order_id=None):
        self.calls.append((method, path))
        if path == "/graphql":
            q, v = json["query"], json["variables"]
            if "recipeSearch" in q:
                term = v["query"]["searchText"]
                for rec_term in ("kip", "gehakt", "zalm", "pasta", "stamppot", "curry",
                                 "wraps", "ovenschotel"):
                    if rec_term in term:
                        return self._load(f"graphql-recipesearch__{rec_term}.json")
                return {"data": {"recipeSearch": {"result": []}}}
            if "recipe(id" in q:
                body = self._load(f"graphql-recipe__{v['id']}-s{v['servings']}.json")
                return body or {"data": {"recipe": None}}
            if "bonusPromotions" in q:
                body = self._load(f"graphql-bonuspromotions__{self.PERIOD}-{v['id']}.json")
                return body or {"data": {"bonusPromotions": []}}
        if path.startswith("/mobile-services/bonuspage/v3/metadata"):
            return self._load("bonus-metadata__v3.json")
        if path.startswith("/mobile-services/bonuspage/v2/section"):
            cat = parse_qs(urlparse(path).query)["category"][0]
            return self._load(f"bonus-section__{self.PERIOD}-{_slug(cat)}.json") or {}
        if path.startswith("/mobile-services/product/search/v2"):
            q = parse_qs(urlparse(path).query)["query"][0]
            return self._load(f"product-search__{_slug(q)}.json") or {"products": []}
        raise AssertionError(f"unexpected call {method} {path}")


async def test_picked_dishes_on_recorded_fixtures(monkeypatch):
    """The recorded folder gives search terms for the choice list, and recorded recipes
    picked on five evenings become a menu with AH products."""
    monkeypatch.setattr(ahclient, "CALL_DELAY", 0)
    client = FixtureAhClient()
    periods = await client.bonus_periods()
    bonus_list = await client.bonus_offers(periods[1])
    assert bonus_list and planner.search_terms(bonus_list)
    ids = sorted({int(f.name.split("__")[1].split("-")[0])
                  for f in FIX.glob("graphql-recipe__*-s2.json")})[:5]
    plan = await menu(client, bonus_list, dict(zip((WO, DO, VR, ZA, ZO), ids)))
    assert [d.recipe.id for d in plan.dishes] == ids
    assert plan.basket and set(plan.basket) <= set(plan.products)
    assert all(d.cuisine for d in plan.dishes)


# --- fix round 1 -----------------------------------------------------------------------

KIP11 = bonus(2, "AH Scharrelkipfilet", regular=4.0, now=4.0, mech="1+1 gratis")


async def test_multibuy_deal_scores_its_potential_for_one_pack():
    r = Recipe(1, "Curry met kip", "u", 30, None, None, ["hoofdgerecht"], [], 2,
               [("500 g scharrelkipfilet", "scharrelkipfilet")], [], None)
    score, _ = await planner._score(r, [KIP11], PANTRY)
    assert score == 2.0  # half of the 1+1 deal: another dish may complete it
    client = FakeClient([R(301, "Stamppot met kip", [(500, "g", "scharrelkipfilet")])])
    plan = await menu(client, [KIP11], {WO: 301})
    assert plan.savings == 0.0  # what is really saved is still decided by the basket


async def test_rescale_never_buys_pantry_items():
    client = FakeClient([
        R(320, "Stamppot met gehakt", [(500, "g", "rundergehakt"), (2, "el", "olijfolie")]),
    ])
    client.noname = {"olijfolie"}  # name missing: only the text says what it is
    plan = await menu(client, [GEHAKT], {WO: 320})
    assert plan.dishes[0].pantry == ["2 el olijfolie"]
    new = await rescale_dish(client, plan, WO, 4)
    assert new.dishes[0].pantry == ["4 el olijfolie"]
    assert set(new.basket) == {1}
    assert not any("olie" in q for q in client.product_calls)


def test_zero_saving_offer_is_not_searched():
    biefstuk = Product(5, "AH Biefstuk 2 stuks", None, 9.29, 2.69, True,
                       "100 GRAM VOOR 2.69", True, "Vlees")
    assert planner.search_terms([biefstuk, GEHAKT]) == ["gehakt"]


async def test_amounts_are_merged_before_rounding_to_packs():
    # 200 g + 200 g of gehakt fits in one pack of 500 g: buy 1, not 2
    client = FakeClient([
        R(340, "Stamppot met gehakt", [(200, "g", "rundergehakt")]),
        R(341, "Pasta met gehakt", [(200, "g", "rundergehakt")]),
    ])
    plan = await menu(client, [GEHAKT], {WO: 340, DO: 341})
    assert len(plan.dishes) == 2
    assert [d.lines[0].qty for d in plan.dishes] == [1, 1]  # each dish alone: one pack
    assert plan.basket[1] == 1
    assert plan.savings == 3.0
    assert [d.savings for d in plan.dishes] == [1.5, 1.5]


async def test_pack_size_from_title_when_card_has_none():
    # most bonus cards have no salesUnitSize; "3 kilo" in the title still counts
    aardappel = bonus(9, "AH Kruimige aardappelen 3 kilo", unit=None)
    client = FakeClient([
        R(350, "Stamppot met prei", [(800, "g", "kruimige aardappelen")]),
        R(351, "Curry met aardappel", [(800, "g", "kruimige aardappelen")]),
    ])
    plan = await menu(client, [aardappel], {WO: 350, DO: 351})
    assert len(plan.dishes) == 2
    assert plan.basket[9] == 1


async def test_restore_dish_brings_back_the_previous_recipe():
    client = FakeClient([
        R(180, "Stamppot met gehakt", [(500, "g", "rundergehakt"), (1, "stuks", "ui")]),
        R(181, "Curry met kip", [(500, "g", "scharrelkipfilet")]),
    ])
    plan = await menu(client, [GEHAKT, KIP], {WO: 180, DO: 181}, persons={WO: 3})
    wo = dish_on(plan, WO)
    swapped = await rescale_dish(client, plan, WO, 3)  # stand-in for a swap: same shape
    back = await planner.restore_dish(client, swapped, WO, wo.recipe.id, [GEHAKT, KIP])
    d = dish_on(back, WO)
    assert d.recipe.id == wo.recipe.id and d.persons == 3
    assert (wo.recipe.id, 3) in client.recipe_calls
    assert dish_on(back, DO).recipe.id == dish_on(plan, DO).recipe.id
    assert back.basket == plan.basket and back.savings == plan.savings
    assert d.cuisine == wo.cuisine and d.is_fish == wo.is_fish


# --- Gerecht weghalen -------------------------------------------------------------------


async def test_remove_dish_leaves_no_dish_and_keeps_the_rest():
    from app.planner import put_dish, remove_dish
    client = FakeClient([
        R(120, "Curry met kip", [(1000, "g", "scharrelkipfilet")]),
        R(122, "Stamppot met gehakt", [(500, "g", "rundergehakt")]),
    ])
    plan = await menu(client, [GEHAKT, KIP], {WO: 120, DO: 122})
    gone = dish_on(plan, WO)
    new = remove_dish(plan, WO)
    assert [d.date for d in new.dishes] == [DO]
    assert new.removed_days == [WO] and new.short_by == plan.short_by
    assert all(ln.product.id not in new.basket for ln in gone.lines
               if all(ln.product.id not in [x.product.id for x in d.lines]
                      for d in new.dishes))
    assert new.savings == round(sum(d.savings for d in new.dishes), 2)
    assert remove_dish(new, ZA) is new  # no dish there: nothing changes
    back = Plan.from_dict(json.loads(json.dumps(new.to_dict())))
    assert back.removed_days == [WO]
    # changing another evening keeps the removed one removed
    assert put_dish(new, dish_on(new, DO)).removed_days == [WO]
    assert (await rescale_dish(client, new, DO, 4)).removed_days == [WO]
    assert Plan.from_dict({**new.to_dict(), "removed_days": None}).removed_days == []
    # a dish put back on the removed evening makes it an evening with a dish again
    assert put_dish(new, gone).removed_days == []


# --- restjes (v0.4) ----------------------------------------------------------------


async def test_cook_day_with_leftovers_cooks_for_both_days():
    client = FakeClient([
        R(200, "Stamppot met gehakt", [(500, "g", "rundergehakt")]),
        R(201, "Nasi met kip", [(500, "g", "scharrelkipfilet")]),
    ])
    # a cook day with a leftover night cooks for the persons of both days (2 + 3)
    plan = await menu(client, [GEHAKT, KIP], {WO: 200, VR: 201}, persons={WO: 5},
                      leftovers={WO: DO})
    assert [d.date for d in plan.dishes] == [WO, VR]
    wo = dish_on(plan, WO)
    assert wo.leftover_date == DO and wo.persons == 5
    assert dish_on(plan, VR).leftover_date is None and dish_on(plan, VR).persons == 2
    assert plan.short_by == 0
    back = Plan.from_dict(json.loads(json.dumps(plan.to_dict())))
    assert dish_on(back, WO).leftover_date == DO
    # a log from before v0.4 has no leftover_date
    old = plan.to_dict()
    for x in old["dishes"]:
        del x["leftover_date"]
    assert all(d.leftover_date is None for d in Plan.from_dict(old).dishes)


async def test_remove_dish_drops_its_leftover_night_too():
    from app.planner import put_dish, remove_dish
    client = FakeClient([
        R(250, "Stamppot met gehakt", [(500, "g", "rundergehakt")]),
        R(251, "Nasi met kip", [(500, "g", "scharrelkipfilet")]),
    ])
    plan = await menu(client, [GEHAKT, KIP], {WO: 250, VR: 251}, persons={WO: 4},
                      leftovers={WO: DO})
    gone = dish_on(plan, WO)
    new = remove_dish(plan, WO)
    assert [d.date for d in new.dishes] == [VR]
    assert new.removed_days == [WO, DO]
    assert put_dish(new, gone).removed_days == []


async def test_rescale_keeps_the_leftover_night():
    client = FakeClient([
        R(260, "Stamppot met gehakt", [(500, "g", "rundergehakt")]),
        R(261, "Nasi met kip", [(500, "g", "scharrelkipfilet")]),
    ])
    plan = await menu(client, [GEHAKT, KIP], {WO: 260, VR: 261}, persons={WO: 4},
                      leftovers={WO: DO})
    new = await rescale_dish(client, plan, WO, 10)
    assert dish_on(new, WO).leftover_date == DO and dish_on(new, WO).persons == 10

