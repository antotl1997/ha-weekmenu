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

    # Ruling 19: a paid extra ("2e halve prijs") is never bought beyond the merged need
    kip_half = bonus(2, "AH Scharrelkipfilet", regular=4.0, now=4.0, mech="2e halve prijs")
    paid = FakeClient([
        R(90, "Curry met kip", [(500, "g", "scharrelkipfilet")]),
        R(91, "Pasta met kip", [(1000, "g", "scharrelkipfilet")]),
    ])
    plan = await build_plan(paid, [kip_half], settings([WO, DO]))
    assert plan.basket[2] == 3  # 3 needed: no 4th at half price
    assert plan.savings == 2.0  # one pair reaches the deal


async def test_half_price_second_not_topped_up_when_merged_need_fits_one_pack():
    kip_half = bonus(2, "AH Scharrelkipfilet", regular=4.0, now=4.0, mech="2e halve prijs")
    client = FakeClient([
        R(360, "Curry met kip", [(200, "g", "scharrelkipfilet")]),
        R(361, "Pasta met kip", [(200, "g", "scharrelkipfilet")]),
    ])
    plan = await build_plan(client, [kip_half], settings([WO, DO]))
    assert len(plan.dishes) == 2
    assert plan.basket[2] == 1
    assert plan.savings == 0.0


async def test_free_unit_deal_tops_up_when_shared_even_if_one_pack_is_enough():
    client = FakeClient([
        R(370, "Curry met kip", [(200, "g", "scharrelkipfilet")]),
        R(371, "Pasta met kip", [(200, "g", "scharrelkipfilet")]),
    ])
    plan = await build_plan(client, [KIP11], settings([WO, DO]))
    assert plan.basket[2] == 2  # the second one is free
    assert plan.savings == 4.0


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


# --- fix round 1 -----------------------------------------------------------------------

KIP11 = bonus(2, "AH Scharrelkipfilet", regular=4.0, now=4.0, mech="1+1 gratis")


async def test_multibuy_deal_scores_its_potential_for_one_pack():
    r = Recipe(1, "Curry met kip", "u", 30, None, None, ["hoofdgerecht"], [], 2,
               [("500 g scharrelkipfilet", "scharrelkipfilet")], [], None)
    score, _ = await planner._score(r, [KIP11], PANTRY)
    assert score == 2.0  # half of the 1+1 deal: another dish may complete it
    client = FakeClient([
        R(300, "Stamppot met kalkoen", [(500, "g", "kalkoenfilet")]),  # same, no bonus
        R(301, "Stamppot met kip", [(500, "g", "scharrelkipfilet")]),
    ])
    plan = await build_plan(client, [KIP11], settings([WO]))
    assert [d.recipe.id for d in plan.dishes] == [301]
    assert plan.savings == 0.0  # what is really saved is still decided by the basket


async def test_scoring_uses_most_common_persons():
    client = FakeClient([R(310, "Stamppot met gehakt", [(500, "g", "rundergehakt")])])
    await build_plan(client, [GEHAKT], settings([WO, DO, VR], persons={WO: 4, DO: 4}))
    assert client.recipe_calls[0] == (310, 4)
    assert planner.score_servings(settings([WO, DO], persons={WO: 3, DO: 4}), [WO, DO]) == 4


def _late_failing(fish_variant):
    """40 candidates over 7 evenings where the third cuisine is hard to place.

    The two odd ones come first in the search results, so they are fetched, but they
    score lowest, so the menu search only reaches them after trying the others."""
    if fish_variant:  # the only other cuisines are both fish: at most one may be used
        rs = [R(450, "Pasta met zalm", [(1, "stuks", "ui"), (1, "stuks", "zalm")]),
              R(451, "Curry met zalm", [(1, "stuks", "ui"), (1, "stuks", "zalm")])]
    else:  # the other two cuisines only fit the weekend
        rs = [R(450, "Pasta lang", [(1, "stuks", "ui")], cook=90),
              R(451, "Curry lang", [(1, "stuks", "ui")], cook=90)]
    return rs + [R(400 + i, f"Stamppot {i}", [(500, "g", "rundergehakt")]) for i in range(38)]


@pytest.mark.parametrize("fish_variant", [True, False])
async def test_menu_search_is_fast_on_late_failing_week(fish_variant):
    import time
    week = [WO, DO, VR, ZA, ZO, MA, DI]
    client = FakeClient(_late_failing(fish_variant))
    cands, _ = await planner._gather(client, [GEHAKT], settings(week), set(), week, 40)
    assert len(cands) == 40
    t = time.perf_counter()
    menu, _ = planner._choose_menu(cands, week)
    assert time.perf_counter() - t < 5
    assert menu

    t = time.perf_counter()
    plan = await build_plan(FakeClient(_late_failing(fish_variant)), [GEHAKT],
                            settings(week))
    assert time.perf_counter() - t < 5
    assert plan.dishes
    assert sum(d.is_fish for d in plan.dishes) <= 1
    assert len({d.cuisine for d in plan.dishes}) >= min(3, len(plan.dishes))
    assert all(is_weekend(d.date) or d.cook <= 45 for d in plan.dishes)


async def test_spent_search_budget_gives_best_partial_menu(monkeypatch, caplog):
    monkeypatch.setattr(planner, "_SEARCH_BUDGET", 5)
    client = FakeClient(_late_failing(False))
    plan = await build_plan(client, [GEHAKT], settings([WO, DO, VR, ZA, ZO, MA, DI]))
    assert plan.budget_exhausted is True
    assert "budget" in caplog.text
    assert plan.dishes  # not "no menu possible"
    assert len({d.cuisine for d in plan.dishes}) >= min(3, len(plan.dishes))
    assert plan.short_by == 7 - len(plan.dishes)
    assert Plan.from_dict(json.loads(json.dumps(plan.to_dict()))).budget_exhausted is True


async def test_rescale_never_buys_pantry_items():
    client = FakeClient([
        R(320, "Stamppot met gehakt", [(500, "g", "rundergehakt"), (2, "el", "olijfolie")]),
    ])
    client.noname = {"olijfolie"}  # name missing: only the text says what it is
    plan = await build_plan(client, [GEHAKT], settings([WO]))
    assert plan.dishes[0].pantry == ["2 el olijfolie"]
    new = await rescale_dish(client, plan, WO, 4)
    assert new.dishes[0].pantry == ["4 el olijfolie"]
    assert set(new.basket) == {1}
    assert not any("olie" in q for q in client.product_calls)


async def test_zero_saving_offer_is_not_searched():
    biefstuk = Product(5, "AH Biefstuk 2 stuks", None, 9.29, 2.69, True,
                       "100 GRAM VOOR 2.69", True, "Vlees")
    client = FakeClient([R(330, "Stamppot met gehakt", [(500, "g", "rundergehakt")])])
    await build_plan(client, [biefstuk, GEHAKT], settings([WO]))
    assert client.search_calls == ["gehakt"]


async def test_titles_with_a_cuisine_are_fetched_first():
    odd = [R(500 + i, f"Bowl {i}", [(500, "g", "rundergehakt")]) for i in range(50)]
    client = FakeClient(odd + [R(600, "Stamppot met gehakt", [(500, "g", "rundergehakt")])])
    plan = await build_plan(client, [GEHAKT], settings([WO]))
    assert client.recipe_calls[0] == (600, 2)
    assert [d.recipe.id for d in plan.dishes] == [600]


async def test_amounts_are_merged_before_rounding_to_packs():
    # 200 g + 200 g of gehakt fits in one pack of 500 g: buy 1, not 2
    client = FakeClient([
        R(340, "Stamppot met gehakt", [(200, "g", "rundergehakt")]),
        R(341, "Pasta met gehakt", [(200, "g", "rundergehakt")]),
    ])
    plan = await build_plan(client, [GEHAKT], settings([WO, DO]))
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
    plan = await build_plan(client, [aardappel], settings([WO, DO]))
    assert len(plan.dishes) == 2
    assert plan.basket[9] == 1


async def test_restore_dish_brings_back_the_previous_recipe():
    client = FakeClient([
        R(180, "Stamppot met gehakt", [(500, "g", "rundergehakt"), (1, "stuks", "ui")]),
        R(181, "Curry met kip", [(500, "g", "scharrelkipfilet")]),
    ])
    plan = await build_plan(client, [GEHAKT, KIP], settings([WO, DO], persons={WO: 3}))
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
    plan = await build_plan(client, [GEHAKT, KIP], settings([WO, DO]))
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


def leftover_settings(cook_days, leftovers, persons=None):
    """Settings like make_settings builds them: a cook day with a leftover night
    cooks for the persons of both days."""
    persons = persons or {}
    per = {d: persons.get(d, 2) + (persons.get(leftovers[d], 2) if d in leftovers else 0)
           for d in cook_days}
    return Settings(evenings=list(cook_days), persons=per, pantry=PANTRY,
                    recent_recipe_ids=set(), leftovers=dict(leftovers))


async def test_cook_day_with_leftovers_cooks_for_both_days():
    client = FakeClient([
        R(200, "Stamppot met gehakt", [(500, "g", "rundergehakt")]),
        R(201, "Nasi met kip", [(500, "g", "scharrelkipfilet")]),
    ])
    s = leftover_settings([WO, VR], {WO: DO}, persons={WO: 2, DO: 3})
    plan = await build_plan(client, [GEHAKT, KIP], s)
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


async def test_fish_never_on_a_cook_day_with_leftovers():
    client = FakeClient([
        R(210, "Pasta met zalm", [(400, "g", "zalmfilet")]),  # best saving, fish
        R(211, "Nasi met kip", [(500, "g", "scharrelkipfilet")]),
        R(212, "Stamppot met worst", [(1, "stuks", "rookworst")]),
    ])
    s = leftover_settings([WO, VR, ZA], {WO: DO})
    plan = await build_plan(client, [ZALM, KIP, WORST], s)
    assert len(plan.dishes) == 3
    assert not dish_on(plan, WO).is_fish
    assert sum(d.is_fish for d in plan.dishes) == 1  # the fish moved to another day
    # only fish left for the leftover day: it stays without a dish
    client2 = FakeClient([R(220, "Curry met zalm", [(400, "g", "zalmfilet")])])
    plan2 = await build_plan(client2, [ZALM], leftover_settings([WO], {WO: DO}))
    assert plan2.dishes == [] and plan2.short_by == 1


async def test_swap_keeps_the_leftover_night_and_persons():
    client = FakeClient([
        R(230, "Stamppot met gehakt", [(500, "g", "rundergehakt")]),
        R(231, "Nasi met kip", [(500, "g", "scharrelkipfilet")]),
        R(232, "Pasta met worst", [(1, "stuks", "rookworst")]),
    ])
    s = leftover_settings([WO, VR, ZA], {WO: DO}, persons={WO: 2, DO: 3})
    plan = await build_plan(client, [GEHAKT, KIP, WORST], s)
    old = dish_on(plan, WO)
    client.add(
        R(240, "Curry met zalm", [(400, "g", "zalmfilet")], cook=20),  # fish: not here
        R(241, "Wraps met kip", [(500, "g", "scharrelkipfilet")], cook=30),
    )
    new = await swap_dish(client, plan, WO, [GEHAKT, KIP, WORST, ZALM], s,
                          exclude={old.recipe.id})
    nd = dish_on(new, WO)
    assert nd.recipe.id != old.recipe.id and not nd.is_fish
    assert nd.leftover_date == DO and nd.persons == 5


async def test_remove_dish_drops_its_leftover_night_too():
    from app.planner import put_dish, remove_dish
    client = FakeClient([
        R(250, "Stamppot met gehakt", [(500, "g", "rundergehakt")]),
        R(251, "Nasi met kip", [(500, "g", "scharrelkipfilet")]),
    ])
    plan = await build_plan(client, [GEHAKT, KIP], leftover_settings([WO, VR], {WO: DO}))
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
    plan = await build_plan(client, [GEHAKT, KIP], leftover_settings([WO, VR], {WO: DO}))
    new = await rescale_dish(client, plan, WO, 10)
    assert dish_on(new, WO).leftover_date == DO and dish_on(new, WO).persons == 10


# --- v0.6: the planner off the event loop ---------------------------------------------


async def _heavy_cands():
    """30 candidates over 5 cuisines but only 6 different recipes: 7 evenings can never
    be filled, and no cuisine rule prunes it, so the search runs until its budget."""
    import dataclasses
    week = [WO, DO, VR, ZA, ZO, MA, DI]
    client = FakeClient([R(400 + i, f"Stamppot {i}", [(500, "g", "rundergehakt")])
                         for i in range(6)])
    cands, _ = await planner._gather(client, [GEHAKT], settings(week), set(), week, 40)
    return week, [dataclasses.replace(c, cuisine=cu, order=k * 10 + j)
                  for k, cu in enumerate("abcde") for j, c in enumerate(cands)]


async def test_event_loop_stays_responsive_during_a_heavy_menu_search(monkeypatch, caplog):
    """While _choose_menu searches for at least a second, sleep(0.01) tasks on the loop
    keep coming back within 0.2 s. A probe only counts when it came back while the search
    was still running: with the search on the loop itself no probe would.

    The median is checked, not every probe: on Windows a busy worker thread makes an
    occasional loop wake-up take 0.2 to 0.3 s (GIL hand-off and the 15.6 ms timer), also
    with a thread that does nothing but count."""
    import asyncio
    import statistics
    import threading
    import time
    week, many = await _heavy_cands()
    # a fake search budget big enough for about 1.5 s of searching on this machine
    monkeypatch.setattr(planner, "_SEARCH_BUDGET", 20_000)
    t = time.perf_counter()
    planner._choose_menu(many, week)
    per_node = (time.perf_counter() - t) / 20_000
    monkeypatch.setattr(planner, "_SEARCH_BUDGET", max(200_000, int(1.5 / per_node)))

    async def gather(*a, **k):
        return many, 6

    started, finished = threading.Event(), threading.Event()
    real = planner._choose_menu

    def watched(*a, **k):
        started.set()
        try:
            return real(*a, **k)
        finally:
            finished.set()

    monkeypatch.setattr(planner, "_gather", gather)
    monkeypatch.setattr(planner, "_choose_menu", watched)
    caplog.set_level("INFO", logger="app.planner")
    client = FakeClient([R(400 + i, f"Stamppot {i}", [(500, "g", "rundergehakt")])
                         for i in range(6)])
    task = asyncio.create_task(build_plan(client, [GEHAKT], settings(week)))
    while not started.is_set():
        await asyncio.sleep(0.001)
    probes = []
    while not finished.is_set():
        t = time.perf_counter()
        await asyncio.sleep(0.01)
        if not finished.is_set():  # came back while the search still ran
            probes.append(time.perf_counter() - t)
    plan = await task
    assert plan.budget_exhausted and plan.dishes
    assert plan.search_seconds >= 1.0  # _choose_menu really ran that long
    assert len(probes) >= 5, probes  # the loop answered all along
    assert statistics.median(probes) < 0.2, probes
    assert "Planner: menu in" in caplog.text


def test_search_seconds_is_not_stored():
    p = Plan([], {}, {}, 0.0, [], 0, search_seconds=1.5)
    assert "search_seconds" not in p.to_dict()
    assert Plan.from_dict(p.to_dict()) == p  # not compared either
