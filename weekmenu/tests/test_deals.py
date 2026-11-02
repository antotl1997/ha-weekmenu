import pytest

from app.ahclient import Product
from app.deals import Deal, parse_mechanism, qty_for_deal, savings


def prod(regular=4.0, now=None, mech=None, bonus=True):
    return Product(
        id=1, title="x", unit_size=None, regular=regular,
        now=regular if now is None else now, is_bonus=bonus,
        mechanism=mech, orderable=True, category=None,
    )


def test_half_price_second():
    p = prod(regular=4.00, mech="2e halve prijs")
    assert savings(p, 1) == 0 and savings(p, 2) == 2.00 and savings(p, 3) == 2.00


def test_one_plus_one():
    p = prod(regular=3.00, mech="1+1 gratis")
    assert savings(p, 2) == 3.00 and qty_for_deal(p, 1) == 2


def test_two_plus_one():
    p = prod(regular=2.00, mech="2+1 gratis")
    assert savings(p, 3) == 2.00 and savings(p, 2) == 0


def test_n_for():
    p = prod(regular=2.49, mech="2 voor 3.99")
    assert round(savings(p, 2), 2) == 0.99
    assert round(savings(p, 3), 2) == 0.99  # third unit at regular price


def test_percent():
    p = prod(regular=10.00, now=7.50, mech="25% korting")
    assert savings(p, 2) == 5.00


def test_plain_price():
    p = prod(regular=5.99, now=4.49, mech=None)
    assert round(savings(p, 1), 2) == 1.50
    assert parse_mechanism(None) == Deal("unknown")  # no price info on its own


def test_unknown_is_zero():
    assert savings(prod(regular=3, mech="Bonus Box extra"), 5) == 0


def test_none_now_equal_regular_is_unknown():
    p = prod(regular=3.0, now=3.0, mech=None)
    assert savings(p, 2) == 0


def test_qty_for_deal():
    half = prod(mech="2e halve prijs")
    assert qty_for_deal(half, 3) == 4 and qty_for_deal(half, 2) == 2
    assert qty_for_deal(prod(mech="2+1 gratis"), 1) == 3
    assert qty_for_deal(prod(mech="2 voor 3.99"), 1) == 2
    assert qty_for_deal(prod(mech="25% korting", now=3.0), 3) == 3
    assert qty_for_deal(prod(mech="VOOR 1.99", now=1.99), 3) == 3
    assert qty_for_deal(prod(mech="Feestfavoriet"), 3) == 3


def test_n_for_without_saving_does_not_pad_qty():
    p = prod(regular=2.00, mech="2 voor 4.00")
    assert savings(p, 2) == 0 and qty_for_deal(p, 1) == 1


def test_parse_shapes():
    assert parse_mechanism("2e halve prijs") == Deal("nth_discount", n=2, pct=50)
    assert parse_mechanism("2 + 2 gratis") == Deal("x_plus_y", n=2, free=2)
    assert parse_mechanism("2 VOOR 13.99") == Deal("n_for", n=2, total=13.99)
    assert parse_mechanism("2e gratis") == Deal("nth_discount", n=2, pct=100)
    assert parse_mechanism("VOOR 1.99") == Deal("price", total=1.99)


# Distinct strings from the fixtures (bonusMechanism and discount.description).
# (mechanism, regular, now, qty, expected savings; None = unknown deal kind)
FIXTURE_CASES = [
    ("1 EURO KORTING", 5.0, 4.0, 2, 2.00),
    ("1 euro korting", 5.99, 4.99, 1, 1.00),
    ("1 euro korting", 5.0, 5.0, 2, 0.0),
    # "100 GRAM VOOR x": currentPrice is absent or equals the per-100g price,
    # so regular - now would over-count; computing it needs the package weight.
    ("100 GRAM VOOR 0.75", 1.0, 1.0, 2, None),
    ("100 GRAM VOOR 3.42", 4.0, 4.0, 2, None),
    ("15% korting", 4.00, 3.40, 2, 1.20),
    ("25% korting", 4.00, 3.00, 3, 3.00),
    ("30% korting", 10.00, 7.00, 1, 3.00),
    ("40% KORTING", 5.00, 3.00, 2, 4.00),
    ("40% korting", 5.00, 3.00, 2, 4.00),
    ("50% korting", 3.00, 1.50, 2, 3.00),
    ("2 + 2 gratis", 2.00, 2.00, 4, 4.00),
    ("2 + 2 gratis", 2.00, 2.00, 3, 0.0),
    ("2 VOOR 13.99", 8.00, 8.00, 2, 2.01),
    ("2 VOOR 2.99", 2.00, 2.00, 2, 1.01),
    ("2 VOOR 4.00", 2.49, 2.49, 2, 0.98),
    ("2 voor 4.00", 2.49, 2.49, 2, 0.98),
    ("2 VOOR 4.99", 3.00, 3.00, 4, 2.02),
    ("2 voor 2.49", 1.89, 1.89, 2, 1.29),
    ("2e gratis", 3.00, 3.00, 2, 3.00),
    ("2e gratis", 3.00, 3.00, 1, 0.0),
    ("1 + 1 gratis", 2.50, 2.50, 2, 2.50),
    ("4 VOOR 1.99", 0.89, 0.89, 4, 1.57),
    ("VOOR 0.39", 0.59, 0.39, 2, 0.40),
    ("VOOR 0.99", 1.49, 0.99, 1, 0.50),
    ("VOOR 1.99", 2.79, 1.99, 3, 2.40),
    ("VOOR 2.99", 3.99, 2.99, 2, 2.00),
    ("VOOR 3.79", 4.79, 3.79, 1, 1.00),
    ("VOOR 14.99", 19.99, 14.99, 1, 5.00),
    ("VOOR 10.00", 12.00, 10.00, 2, 4.00),
    # personal / unparseable offers: never guess
    ("Feestfavoriet", 3.0, 2.0, 2, None),
    ("Persoonlijke Bonus", 3.0, 2.0, 2, None),
    ("Eerder gekocht", 3.0, 2.0, 2, None),
    ("Bonus Box", 3.0, 2.0, 2, None),
    ("vandaag", 3.0, 2.0, 2, None),
    ("vanaf maandag", 3.0, 2.0, 2, None),
    ("Alle Bonus", 3.0, 2.0, 2, None),
    ("", 3.0, 3.0, 2, None),
]


@pytest.mark.parametrize("mech,regular,now,qty,expected", FIXTURE_CASES)
def test_fixture_mechanisms(mech, regular, now, qty, expected):
    p = prod(regular=regular, now=now, mech=mech)
    if expected is None:
        assert parse_mechanism(mech).kind == "unknown"
        assert savings(p, qty) == 0
    else:
        assert savings(p, qty) == pytest.approx(expected, abs=0.005)
