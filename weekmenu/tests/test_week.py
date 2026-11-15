from datetime import date, datetime

import pytest

from app.week import (
    WEEKDAGEN,
    away_dates,
    bestelling_zin,
    eat_days,
    evenings,
    is_weekend,
    lange_datum,
    menu_week_start,
    plan_days,
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


def test_five_evenings_all_at_home():
    assert [d.day for d in evenings(date(2026, 11, 4), 5)] == [4, 5, 6, 7, 8]


def test_seven_evenings_with_one_away_gives_six():
    assert len(evenings(date(2026, 11, 4), 7, {"zaterdag"})) == 6


def test_evenings_never_go_past_tuesday():
    days = evenings(date(2026, 11, 4), 7)
    assert days[-1] == date(2026, 11, 10)
    assert len(days) == 7


@pytest.mark.parametrize("aantal", [0, 8, -1])
def test_aantal_outside_range_raises(aantal):
    with pytest.raises(ValueError):
        evenings(date(2026, 11, 4), aantal)


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


def test_two_away_days_friday_and_saturday_shift_five_evenings():
    days = evenings(WO, 5, {"vrijdag", "zaterdag"})
    assert [WEEKDAGEN[d.weekday()] for d in days] == [
        "woensdag", "donderdag", "zondag", "maandag", "dinsdag"]


def test_away_never_goes_past_tuesday():
    days = evenings(WO, 7, {"vrijdag", "zaterdag"})
    assert days[-1] == date(2026, 11, 10)
    assert len(days) == 5


def test_zero_on_every_day_gives_no_evenings():
    assert evenings(WO, 5, set(WEEKDAGEN)) == []


def test_away_dates_lists_the_week_in_order():
    assert away_dates(WO, {"maandag", "vrijdag", "fundag"}) == [date(2026, 11, 6), date(2026, 11, 9)]
    assert away_dates(WO, set()) == []


# --- eetdagen, kookmomenten en restjes (v0.4) ---

DO, VR, ZA, ZO, MA, DI = (date(2026, 11, d) for d in (5, 6, 7, 8, 9, 10))


def test_eat_days_are_wednesday_to_tuesday_without_away():
    assert eat_days(WO, {"zaterdag", "zondag", "maandag", "dinsdag"}) == [WO, DO, VR]
    assert len(eat_days(WO, set())) == 7
    assert eat_days(WO, set(WEEKDAGEN)) == []


def test_three_eat_days_two_cooking_moments_give_one_leftover_night():
    assert plan_days([WO, DO, VR], 2) == [(WO, DO), (VR, None)]


def test_cooking_moments_are_bounded_by_the_eat_days():
    assert plan_days([WO, DO, VR], 9) == [(WO, None), (DO, None), (VR, None)]
    assert plan_days([WO, DO, VR], 0) == [(WO, DO)]  # 1 kookmoment: hooguit 1 restjesavond
    assert plan_days([], 3) == []


def test_earliest_cooking_days_get_the_leftovers():
    week = [WO, DO, VR, ZA, ZO, MA, DI]
    assert plan_days(week, 5) == [(WO, DO), (VR, ZA), (ZO, None), (MA, None), (DI, None)]


def test_a_cooking_day_gets_at_most_one_leftover_night():
    week = [WO, DO, VR, ZA, ZO, MA, DI]
    assert plan_days(week, 2) == [(WO, DO), (VR, ZA)]


def test_leftover_night_is_the_next_eat_day_not_the_next_date():
    # niet thuis op donderdag: de restjes van woensdag zijn op vrijdag
    assert plan_days([WO, VR, ZA], 2) == [(WO, VR), (ZA, None)]
