from datetime import date, datetime

import pytest

from app.week import (
    WEEKDAGEN,
    away_dates,
    bestelling_zin,
    evenings,
    is_weekend,
    lange_datum,
    menu_week_start,
)


def dt(y, m, d, h=12, mi=0):
    return datetime(y, m, d, h, mi)


def test_week_start_on_saturday():
    assert menu_week_start(dt(2026, 10, 31, 9)) == date(2026, 11, 4)


def test_week_start_on_sunday():
    assert menu_week_start(dt(2026, 11, 1, 8)) == date(2026, 11, 4)


def test_week_start_on_wednesday():
    assert menu_week_start(dt(2026, 11, 4, 12)) == date(2026, 11, 11)


def test_week_start_on_monday():
    assert menu_week_start(dt(2026, 11, 2, 10)) == date(2026, 11, 11)


def test_week_start_on_tuesday():
    assert menu_week_start(dt(2026, 11, 3, 23)) == date(2026, 11, 11)


def test_week_start_on_thursday():
    assert menu_week_start(dt(2026, 11, 5, 9)) == date(2026, 11, 11)


def test_week_start_on_next_sunday():
    assert menu_week_start(dt(2026, 11, 8, 18)) == date(2026, 11, 11)


def test_five_evenings_no_parents():
    assert [d.day for d in evenings(date(2026, 11, 4), 5, "Niet deze week")] == [4, 5, 6, 7, 8]


def test_parents_on_friday_shifts():
    assert [d.day for d in evenings(date(2026, 11, 4), 5, "vrijdag")] == [4, 5, 7, 8, 9]


def test_seven_evenings_with_parents_gives_six():
    assert len(evenings(date(2026, 11, 4), 7, "zaterdag")) == 6


def test_evenings_never_go_past_tuesday():
    days = evenings(date(2026, 11, 4), 7, "Niet deze week")
    assert days[-1] == date(2026, 11, 10)
    assert len(days) == 7


def test_unknown_parents_value_raises():
    with pytest.raises(ValueError):
        evenings(date(2026, 11, 4), 5, "fundag")


@pytest.mark.parametrize("aantal", [0, 8, -1])
def test_aantal_outside_range_raises(aantal):
    with pytest.raises(ValueError):
        evenings(date(2026, 11, 4), aantal, "Niet deze week")


def test_weekdagen_order():
    assert WEEKDAGEN == ["maandag", "dinsdag", "woensdag", "donderdag", "vrijdag", "zaterdag", "zondag"]


def test_is_weekend():
    assert is_weekend(date(2026, 11, 6))   # vrijdag
    assert is_weekend(date(2026, 11, 7))   # zaterdag
    assert is_weekend(date(2026, 11, 8))   # zondag
    assert not is_weekend(date(2026, 11, 4))  # woensdag
    assert not is_weekend(date(2026, 11, 5))  # donderdag


def test_lange_datum_writes_weekday_and_month_in_dutch():
    assert lange_datum(date(2026, 11, 11)) == "woensdag 11 november"
    assert lange_datum(date(2026, 11, 6)) == "vrijdag 6 november"
    assert lange_datum(date(2027, 3, 1)) == "maandag 1 maart"


def test_bestelling_zin_names_the_delivery_wednesday():
    assert bestelling_zin(date(2026, 11, 11)) == "Voor de bestelling van woensdag 11 november"


WO = date(2026, 11, 4)


def test_zero_persons_on_friday_shifts_like_the_parents():
    days = evenings(WO, 5, "Niet deze week", {"vrijdag"})
    assert [WEEKDAGEN[d.weekday()] for d in days] == [
        "woensdag", "donderdag", "zaterdag", "zondag", "maandag"]


def test_zero_on_friday_and_parents_on_saturday():
    days = evenings(WO, 5, "zaterdag", {"vrijdag"})
    assert [WEEKDAGEN[d.weekday()] for d in days] == [
        "woensdag", "donderdag", "zondag", "maandag", "dinsdag"]


def test_zero_persons_never_goes_past_tuesday():
    days = evenings(WO, 7, "Niet deze week", {"vrijdag", "zaterdag"})
    assert days[-1] == date(2026, 11, 10)
    assert len(days) == 5


def test_zero_on_every_day_gives_no_evenings():
    assert evenings(WO, 5, "Niet deze week", set(WEEKDAGEN)) == []
    assert evenings(WO, 5, "vrijdag", set(WEEKDAGEN) - {"vrijdag"}) == []


def test_away_dates_lists_the_week_in_order():
    assert away_dates(WO, {"maandag", "vrijdag", "fundag"}) == [date(2026, 11, 6), date(2026, 11, 9)]
    assert away_dates(WO, set()) == []
