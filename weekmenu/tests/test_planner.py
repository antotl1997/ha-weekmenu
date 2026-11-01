import json
import re
from datetime import date
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

import app.ahclient as ahclient
from app import planner
from app.ahclient import AhApiError, AhClient, AhLoginExpired, Product, Recipe, RecipeSummary
from app.planner import (
    Plan,
    Settings,
    build_plan,
    core_word,
    cuisine_of,
    rescale_dish,
    swap_dish,
)
from app.week import is_weekend

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
                ings.append((name, name))
            else:
                ings.append((f"{qty * f:g} {unit} {name}".replace("  ", " "), name))
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


def settings(evenings, persons=None, recent=()):
    return Settings(
        evenings=list(evenings),
        persons={d: 2 for d in evenings} | (persons or {}),
        pantry=PANTRY,
        recent_recipe_ids=set(recent),
    )


GEHAKT = bonus(1, "AH Rundergehakt", regular=6.0, now=3.0)  # saves 3.00 per pack
KIP = bonus(2, "AH Scharrelkipfilet", regular=5.0, now=3.5)  # saves 1.50
ZALM = bonus(3, "AH Zalmfilet", regular=8.0, now=5.0, cat="Vis")  # saves 3.00
WORST = bonus(4, "AH Rookworst", regular=3.0, now=2.0, unit="275 g")  # saves 1.00


def dish_on(plan, day):
    return next(d for d in plan.dishes if d.date == day)


# --- the brief's tests -----------------------------------------------------------


async def test_highest_savings_wins_when_constraints_allow():
    client = FakeClient([
        R(10, "Stamppot met kip",
          [(500, "g", "scharrelkipfilet"), (1, "kg", "kruimige aardappel")]),
        R(11, "Stamppot met gehakt",
          [(500, "g", "rundergehakt"), (1, "kg", "kruimige aardappel")]),
    ])
    plan = await build_plan(client, [GEHAKT, KIP], settings([WO]))
    assert [d.recipe.id for d in plan.dishes] == [11]
    assert plan.dishes[0].savings == 3.0
    assert plan.savings == 3.0
    assert plan.short_by == 0


async def test_weekday_rejects_cook_over_45_weekend_accepts():
    client = FakeClient([
        R(20, "Lasagne met gehakt", [(500, "g", "rundergehakt")], cook=60),
        R(21, "Nasi met kip", [(500, "g", "scharrelkipfilet")], cook=30),
    ])
    plan = await build_plan(client, [GEHAKT, KIP], settings([WO, VR]))
    assert dish_on(plan, WO).recipe.id == 21
    assert dish_on(plan, VR).recipe.id == 20
    assert dish_on(plan, VR).cook == 60


async def test_at_least_three_cuisines_in_five_evenings():
    gehakt = [
        R(30 + i, f"Stamppot {i} met gehakt", [(500, "g", "rundergehakt")]) for i in range(5)
    ]
    client = FakeClient(gehakt + [
        R(40, "Pasta met kip", [(500, "g", "scharrelkipfilet")]),
        R(41, "Curry met kip", [(500, "g", "scharrelkipfilet")]),
    ])
    evenings = [WO, DO, VR, ZA, ZO]
    plan = await build_plan(client, [GEHAKT, KIP], settings(evenings))
    assert len(plan.dishes) == 5 and plan.short_by == 0
    cuisines = [d.cuisine for d in plan.dishes]
    assert len(set(cuisines)) == 3
    assert cuisines.count("Hollandse pot") == 3
    assert {"pasta en Italiaans", "Aziatisch"} <= set(cuisines)


async def test_at_most_one_fish():
    client = FakeClient([
        R(50, "Pasta met zalm", [(400, "g", "zalmfilet")]),
        R(51, "Curry met zalm", [(400, "g", "zalmfilet")]),
        R(52, "Wraps met zalm", [(400, "g", "zalmfilet")]),
        R(53, "Stamppot met rookworst", [(1, "stuks", "rookworst")]),
        R(54, "Nasi met kip", [(500, "g", "scharrelkipfilet")]),
        R(55, "Taco's met kip", [(500, "g", "scharrelkipfilet")]),
    ])
    plan = await build_plan(client, [ZALM, KIP, WORST], settings([WO, DO, VR]))
    assert len(plan.dishes) == 3
    assert sum(d.is_fish for d in plan.dishes) == 1
    assert len({d.cuisine for d in plan.dishes}) == 3


async def test_recent_recipes_excluded():
    client = FakeClient([
        R(60, "Stamppot met gehakt", [(500, "g", "rundergehakt")]),
        R(61, "Stamppot met kip", [(500, "g", "scharrelkipfilet")]),
    ])
    plan = await build_plan(client, [GEHAKT, KIP], settings([WO], recent={60}))
    assert [d.recipe.id for d in plan.dishes] == [61]
    assert all(i != 60 for i, _ in client.recipe_calls)


async def test_pantry_items_not_in_basket():
    client = FakeClient([
        R(70, "Stamppot met gehakt", [
            (500, "g", "rundergehakt"), (2, "el", "milde olijfolie"), (None, "", "zout"),
            (None, "", "versgemalen peper"),
        ]),
    ])
    plan = await build_plan(client, [GEHAKT], settings([WO]))
    assert set(plan.basket) == {1}
    assert client.product_calls == []  # pantry is never searched
    assert plan.dishes[0].pantry == ["2 el milde olijfolie", "zout", "versgemalen peper"]


async def test_shared_product_merged_once():
    client = FakeClient([
        R(80, "Stamppot met gehakt", [(500, "g", "rundergehakt"), (1, "stuks", "ui")]),
        R(81, "Curry met kip", [(500, "g", "scharrelkipfilet"), (1, "stuks", "ui")]),
        R(82, "Pasta met zalm", [(400, "g", "zalmfilet"), (1, "stuks", "ui")]),
    ])
    plan = await build_plan(client, [GEHAKT, KIP, ZALM], settings([WO, DO, VR]))
    assert len(plan.dishes) == 3
    ui = [pid for pid, p in plan.products.items() if p.title == "Ui"]
    assert len(ui) == 1
    assert plan.basket[ui[0]] == 3
    assert client.product_calls.count("ui") == 1  # cached per name


async def test_smart_topup_only_when_shared():
    kip11 = bonus(2, "AH Scharrelkipfilet", regular=4.0, now=4.0, mech="1+1 gratis")
    one = FakeClient([
        R(90, "Curry met kip", [(500, "g", "scharrelkipfilet")]),
    ])
    plan = await build_plan(one, [kip11], settings([WO]))
    assert plan.basket[2] == 1
    assert plan.savings == 0.0  # the deal is not reached, so it saves nothing

    shared = FakeClient([
        R(90, "Curry met kip", [(500, "g", "scharrelkipfilet")]),
        R(91, "Pasta met kip", [(500, "g", "scharrelkipfilet")]),
    ])
    plan = await build_plan(shared, [kip11], settings([WO, DO]))
    assert plan.basket[2] == 2
    assert plan.savings == 4.0
    assert [d.savings for d in plan.dishes] == [2.0, 2.0]

    three = FakeClient([
        R(90, "Curry met kip", [(500, "g", "scharrelkipfilet")]),
        R(91, "Pasta met kip", [(1000, "g", "scharrelkipfilet")]),
    ])
    plan = await build_plan(three, [kip11], settings([WO, DO]))
    assert plan.basket[2] == 4  # 3 needed, shared: top up to the next 1+1
    assert plan.savings == 8.0
    by_id = {d.recipe.id: d.savings for d in plan.dishes}
    assert by_id == {90: pytest.approx(2.67, abs=0.01), 91: pytest.approx(5.33, abs=0.01)}


async def test_too_few_candidates_sets_short_by():
    client = FakeClient([
        R(100, "Stamppot met gehakt", [(500, "g", "rundergehakt")], cook=30),
        R(101, "Lasagne met gehakt", [(500, "g", "rundergehakt")], cook=60),
        R(102, "Curry met kip", [(500, "g", "scharrelkipfilet")], cook=90),
    ])
    plan = await build_plan(client, [GEHAKT, KIP], settings([WO, DO, MA]))
    assert [d.recipe.id for d in plan.dishes] == [100]
    assert plan.short_by == 2
    assert all(d.cook <= 45 for d in plan.dishes)


async def test_persons_per_evening_scale_recipe():
    client = FakeClient([
        R(110, "Stamppot met gehakt", [(500, "g", "rundergehakt")]),
    ])
    plan = await build_plan(client, [GEHAKT], settings([DO], persons={DO: 4}))
    assert (110, 4) in client.recipe_calls
    d = plan.dishes[0]
    assert d.persons == 4
    assert d.lines[0].qty == 2  # 1000 g in packs of 500 g
    assert plan.basket[1] == 2
    assert d.savings == 6.0


async def test_swap_keeps_other_dishes_and_rules():
    client = FakeClient([
        R(120, "Curry met kip", [(1000, "g", "scharrelkipfilet")]),  # Wednesday (kip)
        R(121, "Pasta met zalm", [(400, "g", "zalmfilet")]),  # the week's fish
        R(122, "Stamppot met gehakt", [(500, "g", "rundergehakt")]),
        R(123, "Wraps met worst", [(1, "stuks", "rookworst")]),
        R(124, "Stamppot met worst", [(1, "stuks", "rookworst")]),
    ])
    evenings = [WO, DO, VR, ZA, ZO]
    plan = await build_plan(client, [GEHAKT, KIP, ZALM, WORST], settings(evenings))
    assert len(plan.dishes) == 5
    old = dish_on(plan, WO)
    assert not old.is_fish
    before = {d.date: d.recipe.id for d in plan.dishes}

    client.add(
        R(130, "Curry met garnalen", [(400, "g", "zalmfilet")], cook=20),  # second fish
        R(131, "Nasi met gehakt", [(1000, "g", "rundergehakt")], cook=90),  # too long
        R(132, "Nasi met kip", [(500, "g", "scharrelkipfilet")], cook=30),
    )
    new = await swap_dish(client, plan, WO, [GEHAKT, KIP, ZALM, WORST], settings(evenings),
                          exclude={old.recipe.id})
    assert dish_on(new, WO).recipe.id == 132
    assert {d.date: d.recipe.id for d in new.dishes if d.date != WO} == {
        k: v for k, v in before.items() if k != WO
    }
    assert sum(d.is_fish for d in new.dishes) == 1
    assert dish_on(new, WO).cook <= 45
    # basket follows the new menu
    assert new.basket[2] == 1 and new.short_by == 0


async def test_swap_respects_cuisine_rule_for_week():
    client = FakeClient([
        R(140, "Pasta met gehakt", [(500, "g", "rundergehakt")]),
        R(141, "Curry met kip", [(500, "g", "scharrelkipfilet")]),
        R(142, "Stamppot met worst", [(1, "stuks", "rookworst")]),
    ])
    evenings = [WO, DO, VR]
    plan = await build_plan(client, [GEHAKT, KIP, WORST], settings(evenings))
    assert len({d.cuisine for d in plan.dishes}) == 3
    day = next(d.date for d in plan.dishes if d.recipe.id == 140)
    client.add(
        R(150, "Hutspot met gehakt", [(1000, "g", "rundergehakt")]),  # would leave 2 cuisines
        R(151, "Taco's met gehakt", [(500, "g", "rundergehakt")]),
    )
    new = await swap_dish(client, plan, day, [GEHAKT, KIP, WORST], settings(evenings),
                          exclude={140})
    assert dish_on(new, day).recipe.id == 151
    assert len({d.cuisine for d in new.dishes}) == 3


async def test_swap_without_candidate_returns_unchanged():
    client = FakeClient([
        R(160, "Stamppot met gehakt", [(500, "g", "rundergehakt")]),
    ])
    plan = await build_plan(client, [GEHAKT], settings([WO]))
    new = await swap_dish(client, plan, WO, [GEHAKT], settings([WO]), exclude={160})
    assert [d.recipe.id for d in new.dishes] == [160]
    assert new.basket == plan.basket and new.savings == plan.savings
    assert new.short_by == 1


async def test_rescale_recomputes_basket_and_savings():
    client = FakeClient([
        R(170, "Stamppot met gehakt", [
            (500, "g", "rundergehakt"), (1, "stuks", "ui"), (2, "el", "olijfolie"),
        ]),
    ])
    plan = await build_plan(client, [GEHAKT], settings([WO]))
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


async def test_ah_calls_are_bounded():
    def name(i):  # 60 different words, no core word among them
        return "quux" + chr(97 + i // 26) + chr(97 + i % 26)

    bonuses = [bonus(1000 + i, f"AH {name(i)}", regular=5.0 + i, now=1.0) for i in range(60)]
    kinds = ["Stamppot", "Pasta", "Curry", "Wraps"]
    recipes = [
        R(2000 + i, f"{kinds[i % 4]} {i}", [(500, "g", name(i % 60))]) for i in range(200)
    ]
    client = FakeClient(recipes)
    plan = await build_plan(client, bonuses, settings([WO, DO, VR, ZA, ZO, MA, DI],
                                                       persons={ZA: 4, ZO: 3}))
    assert len(client.search_calls) <= planner.MAX_RECIPE_SEARCHES == 25
    assert len(client.recipe_calls) <= planner.MAX_RECIPE_DETAILS == 40
    assert plan is not None


async def test_unknown_cuisine_is_excluded():
    client = FakeClient([
        R(180, "Shakshuka met feta", [(500, "g", "rundergehakt")]),
        R(181, "Stamppot met kip", [(500, "g", "scharrelkipfilet")]),
    ])
    plan = await build_plan(client, [GEHAKT, KIP], settings([WO]))
    assert [d.recipe.id for d in plan.dishes] == [181]


async def test_only_main_courses():
    client = FakeClient([
        R(190, "Stamppot met gehakt", [(500, "g", "rundergehakt")], courses=("lunch",)),
        R(191, "Stamppot met kip", [(500, "g", "scharrelkipfilet")]),
    ])
    plan = await build_plan(client, [GEHAKT, KIP], settings([WO]))
    assert [d.recipe.id for d in plan.dishes] == [191]


async def test_failing_recipe_is_skipped_but_login_error_stops():
    client = FakeClient([
        R(200, "Stamppot met gehakt", [(500, "g", "rundergehakt")]),
        R(201, "Stamppot met kip", [(500, "g", "scharrelkipfilet")]),
    ])
    client.fail_ids = {200}
    plan = await build_plan(client, [GEHAKT, KIP], settings([WO]))
    assert [d.recipe.id for d in plan.dishes] == [201]

    class Expired(FakeClient):
        async def search_recipes(self, text, size=30):
            raise AhLoginExpired("verlopen", 401)

    with pytest.raises(AhLoginExpired):
        await build_plan(Expired([]), [GEHAKT], settings([WO]))


async def test_ingredient_without_product_goes_to_not_found():
    client = FakeClient(
        [R(210, "Stamppot met gehakt", [(500, "g", "rundergehakt"),
                                        (None, "", "snufje nootmuskaat")])],
        missing={"snufje nootmuskaat"},
    )
    plan = await build_plan(client, [GEHAKT], settings([WO]))
    assert plan.not_found == ["snufje nootmuskaat"]
    assert plan.dishes[0].not_found == ["snufje nootmuskaat"]
    assert len(plan.dishes) == 1


async def test_fish_and_chicken_first_two_evenings_and_long_recipe_in_weekend():
    client = FakeClient([
        R(220, "Stamppot met gehakt", [(1000, "g", "rundergehakt")], cook=30),
        R(221, "Pasta met zalm", [(400, "g", "zalmfilet")], cook=30),
        R(222, "Curry met kip", [(500, "g", "scharrelkipfilet")], cook=30),
        R(223, "Wraps met worst", [(1, "stuks", "rookworst")], cook=40),
        R(224, "Stoofvlees uit de oven", [(1, "stuks", "rookworst")], cook=120),
    ])
    plan = await build_plan(client, [GEHAKT, KIP, ZALM, WORST],
                            settings([WO, DO, VR, ZA, ZO]))
    first_two = {dish_on(plan, WO).recipe.id, dish_on(plan, DO).recipe.id}
    assert first_two == {221, 222}
    assert dish_on(plan, VR).recipe.id in (220, 223, 224)
    assert next(d for d in plan.dishes if d.recipe.id == 224).date in (VR, ZA, ZO)


async def test_monday_gets_least_fresh_recipe():
    client = FakeClient([
        R(230, "Stamppot met gehakt", [(500, "g", "rundergehakt"), (1, "stuks", "verse prei"),
                                       (1, "stuks", "verse peterselie")]),
        R(231, "Nasi uit de diepvries", [(500, "g", "rundergehakt"),
                                         (1, "zak", "diepvries nasi-bamigroente"),
                                         (1, "pak", "witte snelkookrijst")]),
        R(232, "Pasta met kip", [(500, "g", "scharrelkipfilet"), (1, "stuks", "verse basilicum")]),
    ])
    plan = await build_plan(client, [GEHAKT, KIP], settings([WO, DO, MA]))
    assert dish_on(plan, MA).recipe.id == 231


async def test_dish_keeps_summary_times_and_image():
    client = FakeClient([
        R(240, "Ovenschotel met gehakt", [(500, "g", "rundergehakt")], cook=20, oven=40, wait=5),
    ])
    plan = await build_plan(client, [GEHAKT], settings([WO]))
    d = plan.dishes[0]
    assert (d.cook, d.oven, d.wait) == (20, 40, 5)
    assert d.recipe.image and d.recipe.url and d.recipe.steps


async def test_plan_round_trips_through_json():
    client = FakeClient([
        R(250, "Stamppot met gehakt", [(500, "g", "rundergehakt"), (2, "el", "olijfolie"),
                                       (None, "", "nootmuskaat")], oven=10),
        R(251, "Pasta met zalm", [(400, "g", "zalmfilet"), (1, "stuks", "ui")]),
    ], missing={"nootmuskaat"})
    plan = await build_plan(client, [GEHAKT, ZALM], settings([WO, DO]))
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


async def recorded_plan(evenings=(WO, DO, VR, ZA, ZO)):
    client = FixtureAhClient()
    periods = await client.bonus_periods()
    bonus_list = await client.bonus_offers(periods[1])
    plan = await build_plan(client, bonus_list, settings(list(evenings)))
    return client, bonus_list, plan


async def test_build_plan_on_recorded_fixtures(monkeypatch):
    monkeypatch.setattr(ahclient, "CALL_DELAY", 0)
    client, bonus_list, plan = await recorded_plan()
    assert bonus_list
    assert plan.dishes, "the recorded week must give at least one dinner"
    assert len(plan.dishes) + plan.short_by == 5
    for d in plan.dishes:
        assert d.cuisine
        assert is_weekend(d.date) or (d.cook is not None and d.cook <= 45)
    assert sum(d.is_fish for d in plan.dishes) <= 1
    assert len({d.cuisine for d in plan.dishes}) >= min(3, len(plan.dishes))
    graphql = [c for c in client.calls if c[1] == "/graphql"]
    assert len(graphql) < 25 + 40 + 130  # searches + details + bonus groups
