import json
import re
from pathlib import Path

import pytest

from app.ahclient import Product, product_from_card
from app.ingredients import (
    Amount,
    choose_product,
    is_pantry,
    packs_needed,
    parse_ingredient,
    parse_unit_size,
)

FIX = Path(__file__).parent / "fixtures"
PANTRY = ["olie", "zout", "peper", "suiker", "bloem", "azijn"]


def prod(id, title, *, bonus=False, orderable=True, unit="500 g", regular=2.0, now=None):
    return Product(
        id=id,
        title=title,
        unit_size=unit,
        regular=regular,
        now=regular if now is None else now,
        is_bonus=bonus,
        mechanism="25% korting" if bonus else None,
        orderable=orderable,
        category=None,
    )


class FakeClient:
    def __init__(self, results=None):
        self.results = results or {}
        self.queries = []

    async def search_products(self, query, size=20):
        self.queries.append(query)
        return list(self.results.get(query, []))


def slug(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:80] or "x"


class FixtureClient:
    """Serves the recorded product searches; unknown queries find nothing."""

    def __init__(self):
        self.queries = []

    async def search_products(self, query, size=20):
        self.queries.append(query)
        f = FIX / f"product-search__{slug(query)}.json"
        if not f.exists():
            return []
        body = json.loads(f.read_text(encoding="utf-8"))
        return [product_from_card(c) for c in body.get("products") or [] if c.get("webshopId")]


# --- parse_ingredient ---

@pytest.mark.parametrize("text,name,expected", [
    ("500 g rundergehakt", "rundergehakt", Amount(500, "g", "rundergehakt")),
    ("2 middelgrote uien", "middelgrote ui", Amount(2, "stuks", "middelgrote ui")),
    ("1½ teen knoflook", "knoflook", Amount(1.5, "teen", "knoflook")),
    ("2 el sojasaus", "sojasaus", Amount(2, "el", "sojasaus")),
    ("snufje nootmuskaat", "nootmuskaat", Amount(None, None, "nootmuskaat")),
    ("½ el natuurazijn", "natuurazijn", Amount(0.5, "el", "natuurazijn")),
    ("¼ tl venkelzaad", "venkelzaad", Amount(0.25, "tl", "venkelzaad")),
    ("⅛ tl chilivlokken", "chilivlokken", Amount(0.125, "tl", "chilivlokken")),
    ("2.5 g verse tijm", "verse tijm", Amount(2.5, "g", "verse tijm")),
    ("1,5 l water", "water", Amount(1.5, "l", "water")),
    ("2 tenen knoflook", "knoflook", Amount(2, "teen", "knoflook")),
    ("1 blikje tomatenblokjes", "tomatenblokjes", Amount(1, "blik", "tomatenblokjes")),
    ("½ bos bosui", "bosui", Amount(0.5, "bos", "bosui")),
    ("2 stronken witlof", "witlof", Amount(2, "stuks", "witlof")),
    ("1 limoen, de rasp", "", Amount(1, "stuks", "")),
    ("500g gehakt", "gehakt", Amount(500, "g", "gehakt")),
    ("&#189; limoen", "limoen", Amount(0.5, "stuks", "limoen")),
])
def test_parse_ingredient(text, name, expected):
    assert parse_ingredient(text, name) == expected


def test_parse_ingredient_without_number_has_no_qty():
    a = parse_ingredient("wat zout naar smaak", "zout")
    assert a.qty is None and a.unit is None


def test_parse_ingredient_empty_name_stays_empty():
    a = parse_ingredient("2 el ", "")
    assert a == Amount(2, "el", "")


# --- is_pantry ---

@pytest.mark.parametrize("name", [
    "olie", "olijfolie", "milde olijfolie", "zonnebloemolie", "grof zeezout", "zout",
    "zwarte peper", "basterdsuiker", "rietsuiker", "wittewijnazijn", "rijstazijn", "bloem",
    "Olijfolie", "peper", "witte peper", "versgemalen zwarte peper", "gemalen peper",
    "versgemalen peper", "peper en zout", "cayennepeper", "citroenpeper", "peper uit de molen",
    "olie voor het bakken", "olijfolie om in te bakken", "zout (naar smaak)",
])
def test_pantry_matches(name):
    assert is_pantry(name, PANTRY)


@pytest.mark.parametrize("name", [
    "oliebollen", "peperkoek", "bloemkool", "zoutjes", "paprikapoeder", "ongezouten roomboter",
    "suikerbonen", "rode peper", "kruimige aardappel",
    "chilipeper", "rode chilipeper", "groene chilipeper", "jalapeñopeper", "jalapeño peper",
    "madame jeanette (peper)", "habaneropeper", "gele peper", "Spaanse peper",
    "tonijn in olie", "ansjovis in olie", "zongedroogde tomaten in olie",
    "zilveruitjes in azijn", "augurken in azijn", "kaas met peper",
])
def test_pantry_does_not_match(name):
    assert not is_pantry(name, PANTRY)


@pytest.mark.parametrize("name", ["water", "kraanwater", "kokend water", "lauw water"])
def test_tap_water_is_pantry(name):
    assert is_pantry(name, PANTRY)


def test_mineral_water_is_not_pantry():
    assert not is_pantry("bruisend water met citroen", PANTRY)


def test_pantry_empty_list_and_empty_name():
    assert not is_pantry("olijfolie", [])
    assert not is_pantry("", PANTRY)


def test_pantry_list_is_case_insensitive():
    assert is_pantry("olijfolie", ["Olie"])


# --- parse_unit_size ---

@pytest.mark.parametrize("s,expected", [
    ("500 g", (500, "g")),
    ("1 kg", (1000, "g")),
    ("4 stuks", (4, "stuks")),
    ("per stuk", (1, "stuks")),
    ("ca. 400 g", (400, "g")),
    ("2 x 125 g", (250, "g")),
    ("1 l", (1000, "ml")),
    ("1,5 l", (1500, "ml")),
    ("750 ml", (750, "ml")),
    ("33 cl", (330, "ml")),
    ("2 x 1 l", (2000, "ml")),
    ("Bijv. White renew - 250 milliliter", (250, "ml")),
    ("Los", None),
    ("Los, per kilo", None),
    (None, None),
    ("", None),
])
def test_parse_unit_size(s, expected):
    assert parse_unit_size(s) == expected


# --- packs_needed ---

def test_packs_rounds_up():
    assert packs_needed(Amount(750, "g", "gehakt"), (500, "g")) == 2


def test_packs_exact_is_not_rounded_up():
    assert packs_needed(Amount(500, "g", "gehakt"), (500, "g")) == 1


def test_packs_kg_and_l():
    assert packs_needed(Amount(1.2, "kg", "aardappel"), (1000, "g")) == 2
    assert packs_needed(Amount(1.5, "l", "melk"), (1000, "ml")) == 2


def test_packs_spoons_use_ml():
    assert packs_needed(Amount(2, "el", "sojasaus"), (250, "ml")) == 1
    assert packs_needed(Amount(20, "el", "sojasaus"), (250, "ml")) == 2  # 300 ml
    assert packs_needed(Amount(10, "tl", "x"), (25, "ml")) == 2  # 50 ml


def test_packs_pieces():
    assert packs_needed(Amount(6, "stuks", "ei"), (4, "stuks")) == 2


def test_packs_unknown_unit_is_one():
    assert packs_needed(Amount(2, "bos", "koriander"), None) == 1


def test_packs_not_convertible_is_one():
    assert packs_needed(Amount(3, "stuks", "ui"), (1000, "g")) == 1
    assert packs_needed(Amount(500, "g", "ui"), (4, "stuks")) == 1
    assert packs_needed(Amount(2, "teen", "knoflook"), (1, "stuks")) == 1


def test_packs_no_quantity_is_one():
    assert packs_needed(Amount(None, None, "nootmuskaat"), (50, "g")) == 1


def test_packs_minimum_one():
    assert packs_needed(Amount(10, "g", "tijm"), (500, "g")) == 1


# --- choose_product ---

async def test_choose_prefers_bonus_product():
    bonus = [prod(1, "AH Rode uien", bonus=True)]
    client = FakeClient({"rode ui": [prod(2, "AH Rode ui los")]})
    got = await choose_product(client, Amount(1, "stuks", "rode ui"), bonus)
    assert got.id == 1
    assert client.queries == []  # bonus hit, no search needed


async def test_choose_bonus_must_contain_all_keywords():
    bonus = [prod(1, "AH Rode paprika", bonus=True)]
    client = FakeClient({"rode ui": [prod(2, "AH Rode uien")]})
    got = await choose_product(client, Amount(1, "stuks", "rode ui"), bonus)
    assert got.id == 2


async def test_choose_skips_sold_out_bonus_product():
    bonus = [prod(1, "AH Rode uien", bonus=True, orderable=False)]
    client = FakeClient({"rode ui": [prod(2, "AH Rode uien 500 g")]})
    got = await choose_product(client, Amount(1, "stuks", "rode ui"), bonus)
    assert got.id == 2


async def test_choose_skips_sold_out_search_results():
    client = FakeClient({"ui": [
        prod(1, "AH Uien", orderable=False),
        prod(2, "AH Uien 1 kg"),
    ]})
    got = await choose_product(client, Amount(1, "stuks", "ui"), [])
    assert got.id == 2


async def test_choose_prefers_keyword_match_in_search():
    client = FakeClient({"rode ui": [
        prod(1, "AH Gesneden rode kool"),
        prod(2, "AH Rode uien"),
    ]})
    got = await choose_product(client, Amount(1, "stuks", "rode ui"), [])
    assert got.id == 2


async def test_choose_partial_match_needs_the_head_keyword():
    # "friszoete" is not in any title, but "appel" is: the apple is fine
    client = FakeClient({"friszoete appel": [
        prod(1, "Danone Knijpyoghurt aardbei"),
        prod(2, "AH Elstar appels"),
    ]})
    got = await choose_product(client, Amount(1, "stuks", "friszoete appel"), [])
    assert got.id == 2


async def test_choose_none_when_no_title_has_the_head_keyword():
    # first orderable result would be a wrong product: report "not found" instead
    client = FakeClient({"sambal oelek": [prod(1, "Conimex Sambal badjak"), prod(2, "Ketjap")]})
    assert await choose_product(client, Amount(1, "el", "sambal oelek"), []) is None


async def test_choose_prefers_the_plain_product():
    client = FakeClient({"broccoli": [
        prod(1, "Maggi Ovenschotel broccoli gratin"),
        prod(2, "AH Broccoli"),
    ]})
    got = await choose_product(client, Amount(200, "g", "broccoli"), [])
    assert got.id == 2


async def test_choose_prefers_uncut_over_cut():
    client = FakeClient({"ui": [prod(1, "AH Gesneden uien"), prod(2, "AH Uien")]})
    assert (await choose_product(client, Amount(1, "stuks", "ui"), [])).id == 2


async def test_choose_does_not_trust_bonus_product_of_another_kind():
    bonus = [prod(1, "Ola Festini peer ijs", bonus=True, regular=4.0, now=2.0)]
    client = FakeClient({"peer": [prod(2, "AH Conference peren")]})
    got = await choose_product(client, Amount(1, "stuks", "peer"), bonus)
    assert got.id == 2


async def test_choose_diminutive_and_adjective_endings():
    client = FakeClient({"Catalaans braadworstje": [prod(1, "AH Catalaanse braadworst")]})
    got = await choose_product(client, Amount(3, "stuks", "Catalaans braadworstje"), [])
    assert got.id == 1


async def test_choose_returns_none_when_nothing_found():  # Review Focus 3
    client = FakeClient({})
    assert await choose_product(client, Amount(None, None, "nootmuskaat"), []) is None


async def test_choose_returns_none_when_all_sold_out():
    client = FakeClient({"limoen": [prod(1, "AH Limoenen", orderable=False)]})
    assert await choose_product(client, Amount(1, "stuks", "limoen"), []) is None


async def test_choose_empty_name_is_none_and_does_not_search():
    client = FakeClient({})
    assert await choose_product(client, Amount(1, "stuks", ""), []) is None
    assert await choose_product(client, Amount(1, "stuks", "   "), []) is None
    assert client.queries == []


async def test_choose_dutch_plural_matches_bonus():
    bonus = [
        prod(1, "AH Tomaten", bonus=True),
        prod(2, "AH Uien", bonus=True),
        prod(3, "AH Scharreleieren 10 stuks", bonus=True),
    ]
    c = FakeClient({})
    assert (await choose_product(c, Amount(2, "stuks", "tomaat"), bonus)).id == 1
    assert (await choose_product(c, Amount(2, "stuks", "middelgrote ui"), bonus)).id == 2
    assert (await choose_product(c, Amount(2, "stuks", "middelgroot scharrelei"), bonus)).id == 3


async def test_choose_short_keyword_is_a_whole_word():
    # "ui" must not match inside "kruiden"
    bonus = [prod(1, "AH Kruidenmix", bonus=True)]
    c = FakeClient({})
    assert await choose_product(c, Amount(1, "stuks", "ui"), bonus) is None


async def test_choose_ignores_parenthetical():
    bonus = [prod(1, "AH Paprikamix", bonus=True)]
    c = FakeClient({})
    got = await choose_product(c, Amount(1, "zak", "paprikamix (rood, groen, geel)"), bonus)
    assert got.id == 1


async def test_choose_caches_searches_within_a_round():
    client = FakeClient({"ui": [prod(1, "AH Uien")]})
    cache = {}
    await choose_product(client, Amount(1, "stuks", "ui"), [], cache=cache)
    await choose_product(client, Amount(2, "stuks", "ui"), [], cache=cache)
    assert client.queries == ["ui"]


async def test_choose_without_cache_searches_each_time():
    client = FakeClient({"ui": [prod(1, "AH Uien")]})
    await choose_product(client, Amount(1, "stuks", "ui"), [])
    await choose_product(client, Amount(1, "stuks", "ui"), [])
    assert client.queries == ["ui", "ui"]


# --- recorded fixtures ---

RECORDED = [
    ("middelgrote ui", 2, "stuks"),
    ("knoflook", 1, "teen"),
    ("milde olijfolie", 1, "el"),
    ("broccoli", 250, "g"),
    ("friszoete appel", 1, "stuks"),
    ("halfvolle melk", 35, "ml"),
    ("zuivelspread naturel", 100, "g"),
    ("rode ui", 1, "stuks"),
    ("ketjap manis", 1.5, "el"),
]


@pytest.mark.parametrize("name,qty,unit", RECORDED)
async def test_choose_on_recorded_fixtures_finds_a_product(name, qty, unit):
    got = await choose_product(FixtureClient(), Amount(qty, unit, name), [])
    assert got is not None and got.orderable


async def test_choose_on_recorded_fixtures_ui_is_an_onion():
    got = await choose_product(FixtureClient(), Amount(2, "stuks", "middelgrote ui"), [])
    assert "ui" in got.title.lower()


async def test_choose_on_recorded_fixtures_peer_is_not_a_drink_or_baby_food():
    # AH search for "peer" is mostly juice and baby food; the guard must skip those
    got = await choose_product(FixtureClient(), Amount(0.5, "stuks", "peer"), [])
    assert got is None or (
        "peer" in got.title.lower()
        and got.category not in ("Frisdrank, sappen, water", "Baby en kind")
        and "siroop" not in got.title.lower()
    )


async def test_choose_skips_drinks_snacks_and_baby_food():
    client = FakeClient({"peer": [
        prod(2, "AH Peer sap"),
        prod(3, "AH Peertjes op siroop"),
        prod(4, "AH Conference peren"),
    ]})
    got = await choose_product(client, Amount(1, "stuks", "peer"), [])
    assert got.id == 4


async def test_choose_skips_implausible_category():
    p1 = prod(1, "Van de Boom Peer 6-pack")
    p1 = Product(**{**p1.__dict__, "category": "Frisdrank, sappen, water"})
    p2 = Product(**{**prod(2, "AH Peer").__dict__, "category": "Baby en kind"})
    client = FakeClient({"peer": [p1, p2]})
    assert await choose_product(client, Amount(1, "stuks", "peer"), []) is None


async def test_choose_marker_allowed_when_recipe_asks_for_it():
    client = FakeClient({"appelsap": [prod(1, "AH Appelsap")]})
    got = await choose_product(client, Amount(200, "ml", "appelsap"), [])
    assert got.id == 1
