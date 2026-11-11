"""v0.9: the fixed matching errors (rondetafel 4), each a wrong product in a real order."""

from datetime import date
from pathlib import Path

import pytest
import yaml

from app.ahclient import Product, Recipe
from app.ingredients import Amount, choose_product, parse_ingredient, weight_priced
from app.planner import _fresh_count, _is_chicken, _line, core_word

KEUKENS = Path(__file__).parent.parent / "app" / "keukens.yaml"


def prod(id, title, *, bonus=False, unit="500 g", regular=2.0):
    return Product(id, title, unit, regular, regular, bonus,
                   "25% korting" if bonus else None, True, None)


class FakeClient:
    def __init__(self, results=None):
        self.results = results or {}
        self.queries = []

    async def search_products(self, query, size=20):
        self.queries.append(query)
        return list(self.results.get(query, []))


# --- ranges and fractions ------------------------------------------------------------


@pytest.mark.parametrize("text, qty, unit", [
    ("2-3 el olijfolie", 3.0, "el"),
    ("2 - 3 el olijfolie", 3.0, "el"),
    ("2–3 tl komijn", 3.0, "tl"),
    ("2 à 3 teentjes knoflook", 3.0, "teen"),
    ("2 tot 3 el sojasaus", 3.0, "el"),
    ("1 1/2 el honing", 1.5, "el"),
    ("1 1/2-2 el honing", 2.0, "el"),
    ("½-1 tl chilivlokken", 1.0, "tl"),
    ("100-150 g spinazie", 150.0, "g"),
    ("1,5-2 kg aardappelen", 2.0, "kg"),
])
def test_ranges_use_the_top_and_mixed_fractions_add_up(text, qty, unit):
    a = parse_ingredient(text, "x")
    assert (a.qty, a.unit) == (pytest.approx(qty), unit)


def test_a_dash_without_a_second_number_is_no_range():
    a = parse_ingredient("2 el olie - extra vergine", "olie")
    assert (a.qty, a.unit) == (2.0, "el")


# --- per 100 gram / per kilo without a pack size -----------------------------------


@pytest.mark.parametrize("title", [
    "AH Rundergehakt per 100 gram", "AH Kipfilet per kilo", "Zalmfilet per 100 g",
    "Gehakt per kg",
])
def test_weight_priced_title_without_pack_size(title):
    assert weight_priced(Product(1, title, None, 1.0, 1.0, False, None, True, None))


def test_weight_priced_needs_no_pack_size():
    with_size = Product(1, "AH Rundergehakt per 100 gram", "500 g", 1.0, 1.0, False, None,
                        True, None)
    assert not weight_priced(with_size)
    assert not weight_priced(Product(2, "AH Rundergehakt 500 g", None, 1.0, 1.0, False,
                                     None, True, None))


def test_weight_priced_line_is_one_pack_and_marked_for_checking():
    p = Product(1, "AH Rundergehakt per 100 gram", None, 1.2, 1.2, False, None, True, None)
    ln = _line(p, parse_ingredient("500 g rundergehakt", "rundergehakt"), "500 g rundergehakt")
    assert ln.qty == 1 and ln.fraction is None and ln.check is True
    plain = Product(2, "AH Rundergehakt", "500 g", 4.0, 4.0, False, None, True, None)
    ln = _line(plain, parse_ingredient("750 g rundergehakt", "rundergehakt"), "x")
    assert ln.qty == 2 and ln.check is False


# --- directional compounds -----------------------------------------------------------


async def test_kaas_matches_geitenkaas_not_pindakaas_or_kaasstengels():
    bonus = [prod(1, "Calvé Pindakaas", bonus=True), prod(2, "AH Kaasstengels", bonus=True),
             prod(3, "AH Geitenkaas", bonus=True)]
    p = await choose_product(FakeClient(), Amount(100, "g", "kaas"), bonus)
    assert p.id == 3
    p = await choose_product(FakeClient(), Amount(100, "g", "kaas"), bonus[:2])
    assert p is None


async def test_room_matches_slagroom_and_kookroom_not_roomboter():
    p = await choose_product(FakeClient(), Amount(200, "ml", "room"),
                             [prod(1, "AH Roomboter", bonus=True),
                              prod(2, "AH Slagroom", bonus=True)])
    assert p.id == 2
    client = FakeClient({"room": [prod(3, "AH Roomboter ongezouten"), prod(4, "AH Kookroom")]})
    p = await choose_product(client, Amount(200, "ml", "room"), [])
    assert p.id == 4


async def test_melk_matches_halfvolle_melk_not_melkchocolade():
    client = FakeClient({"melk": [prod(1, "Tony's Melkchocolade"),
                                  prod(2, "AH Halfvolle melk")]})
    p = await choose_product(client, Amount(250, "ml", "melk"), [])
    assert p.id == 2
    client = FakeClient({"melk": [prod(1, "Tony's Melkchocolade")]})
    assert await choose_product(client, Amount(250, "ml", "melk"), []) is None


async def test_compound_still_matches_its_tail():
    client = FakeClient({"kipfilet": [prod(1, "AH Scharrel kipfilet")],
                         "gehakt": [prod(2, "AH Rundergehakt")],
                         "tomaten": [prod(3, "AH Cherrytomaten")]})
    assert (await choose_product(client, Amount(1, "stuks", "kipfilet"), [])).id == 1
    assert (await choose_product(client, Amount(1, "stuks", "gehakt"), [])).id == 2
    assert (await choose_product(client, Amount(1, "stuks", "tomaten"), [])).id == 3


# --- kip ---------------------------------------------------------------------------


@pytest.mark.parametrize("text", ["2 tl kipkruiden", "500 ml kippenbouillon",
                                  "1 blokje kipbouillon"])
def test_kip_seasoning_and_stock_are_not_chicken(text):
    assert not _is_chicken(text)


def test_chicken_is_still_chicken():
    assert _is_chicken("400 g kipdijfilet") and _is_chicken("kip")


# --- distinguishing adjectives ------------------------------------------------------


async def test_rode_ui_does_not_fall_back_on_white_onions():
    client = FakeClient({"rode ui": [prod(1, "AH Witte uien"), prod(2, "AH Uien")]})
    assert await choose_product(client, Amount(1, "stuks", "rode ui"), []) is None
    client = FakeClient({"rode ui": [prod(1, "AH Witte uien"), prod(3, "AH Rode uien")]})
    assert (await choose_product(client, Amount(1, "stuks", "rode ui"), [])).id == 3


async def test_rode_kool_matches_rodekool():
    p = await choose_product(FakeClient(), Amount(1, "stuks", "rode kool"),
                             [prod(1, "AH Rodekool", bonus=True)])
    assert p.id == 1


@pytest.mark.parametrize("name, wrong, right", [
    ("zoete aardappel", "AH Aardappelen", "AH Zoete aardappel"),
    ("gerookte zalm", "AH Zalmfilet", "AH Gerookte zalm"),
    ("gedroogde tijm", "AH Tijm", "Verstegen Gedroogde tijm"),
    ("gele paprika", "AH Rode paprika", "AH Gele paprika"),
])
async def test_adjective_must_be_in_the_title(name, wrong, right):
    client = FakeClient({name: [prod(1, wrong)]})
    assert await choose_product(client, Amount(1, "stuks", name), []) is None
    client = FakeClient({name: [prod(1, wrong), prod(2, right)]})
    assert (await choose_product(client, Amount(1, "stuks", name), [])).id == 2


async def test_verse_rejects_dried_or_smoked_and_takes_the_plain_product():
    client = FakeClient({"verse tijm": [prod(1, "Verstegen Gedroogde tijm"),
                                        prod(2, "AH Tijm")]})
    assert (await choose_product(client, Amount(1, "bos", "verse tijm"), [])).id == 2
    client = FakeClient({"verse tijm": [prod(1, "Verstegen Gedroogde tijm")]})
    assert await choose_product(client, Amount(1, "bos", "verse tijm"), []) is None


# --- keukens.yaml ---------------------------------------------------------------------


def test_room_and_pot_are_no_loose_core_words():
    conf = yaml.safe_load(KEUKENS.read_text(encoding="utf-8"))
    assert "room" not in conf["kernwoorden"] and "pot" not in conf["houdbaar"]
    assert core_word("AH Roomboter ongezouten") != "room"
    assert core_word("AH Kookroom") == "kookroom"


def test_hutspot_is_fresh():
    r = Recipe(1, "Hutspot", "", 30, None, None, ["hoofdgerecht"], [], 2,
               [("1 kg hutspotgroente", "hutspotgroente")], [], None)
    assert _fresh_count(r, []) == 1


async def test_a_form_suffix_keeps_the_compound():
    client = FakeClient({"spek": [prod(1, "AH Spekblokjes")],
                         "kipfilet": [prod(2, "AH Kipfiletreepjes")]})
    assert (await choose_product(client, Amount(100, "g", "spek"), [])).id == 1
    assert (await choose_product(client, Amount(100, "g", "kipfilet"), [])).id == 2
