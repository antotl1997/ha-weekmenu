"""v1.0 point 1: the cooking screen gets the recipe amounts in recipe order, each with
the AH product under it."""

from datetime import date

from app.ahclient import Product, Recipe
from app.ledger import WeekLog
from app.planner import Dish, Line, _finish
from app.web import dish_view

WO = date(2026, 11, 4)


def test_cook_lines_follow_the_recipe_with_the_product_small_under_it():
    kip = Product(1, "AH Kipdijfilet", "400 g", 5.0, 3.0, True, "2e halve prijs", True, None)
    r = Recipe(7, "Kip teriyaki", "", 25, None, None, ["hoofdgerecht"], [], 2,
               [("400 g kipdijfilet", "kipdijfilet"), ("2 el zonnebloemolie", "olie"),
                ("1 tl sesamzaad", "sesamzaad"), ("snufje nootmuskaat", "nootmuskaat")],
               ["Snijd de kip.", "Bak de kip."], None)
    d = Dish(WO, r, "Aziatisch", False, [Line(kip, 1, "400 g kipdijfilet")], 1.5, cook=25,
             pantry=["2 el zonnebloemolie"], skipped=["1 tl sesamzaad"],
             not_found=["snufje nootmuskaat"])
    plan = _finish([d], 0, ["olie"])
    view = dish_view(WeekLog(WO, plan=plan.to_dict()), plan, plan.dishes[0], WO)
    assert view["cook"] == [
        {"text": "400 g kipdijfilet", "product": "AH Kipdijfilet", "qty": 1, "note": None},
        {"text": "2 el zonnebloemolie", "product": None, "qty": 0, "note": "in huis"},
        {"text": "1 tl sesamzaad", "product": None, "qty": 0, "note": "hoeft niet"},
        {"text": "snufje nootmuskaat", "product": None, "qty": 0,
         "note": "niet gevonden bij AH"},
    ]
    assert view["steps"] == ["Snijd de kip.", "Bak de kip."]
