"""Menuweek en kookavonden. Pure logica, geen I/O."""

from collections.abc import Iterable
from datetime import date, datetime, timedelta

WEEKDAGEN = ["maandag", "dinsdag", "woensdag", "donderdag", "vrijdag", "zaterdag", "zondag"]

MAANDEN = ["januari", "februari", "maart", "april", "mei", "juni", "juli", "augustus",
           "september", "oktober", "november", "december"]

_ZONDAG = 6  # date.weekday(): maandag=0 ... zondag=6
_WOENSDAG_NA_ZONDAG = 3  # de woensdag na de zondag


def menu_week_start(now: datetime) -> date:
    """Woensdag waarmee de menuweek begint.

    Dat is de woensdag na de komende zondag, of na deze zondag als het zondag is.
    Zo begint de week van za 31 okt en zo 1 nov op woensdag 4 nov, en de week
    van ma 2 t/m zo 8 nov op woensdag 11 nov.
    """
    today = now.date()
    zondag = today + timedelta(days=(_ZONDAG - today.weekday()) % 7)
    return zondag + timedelta(days=_WOENSDAG_NA_ZONDAG)


def evenings(start: date, aantal: int, away: Iterable[str] = ()) -> list[date]:
    """Kookavonden vanaf `start`, woensdag t/m dinsdag (start + 6).

    De weekdagen in `away` (0 personen, niet thuis eten) worden overgeslagen; de
    volgende dag schuift op. Geeft minder dagen terug als de week er niet genoeg
    heeft, en geen enkele als elke dag wegvalt.
    """
    away = set(away)
    if not 1 <= aantal <= 7:
        raise ValueError(f"aantal avonden moet tussen 1 en 7 liggen, niet {aantal}")

    result: list[date] = []
    for offset in range(7):
        if len(result) == aantal:
            break
        day = start + timedelta(days=offset)
        if WEEKDAGEN[day.weekday()] in away:
            continue
        result.append(day)
    return result


def away_dates(start: date, away: Iterable[str]) -> list[date]:
    """The days of the menu week (start t/m start + 6) whose weekday is in `away`."""
    away = set(away)
    days = (start + timedelta(days=i) for i in range(7))
    return [d for d in days if WEEKDAGEN[d.weekday()] in away]


def is_weekend(d: date) -> bool:
    """Vrijdag, zaterdag of zondag: geen tijdgrens voor de kooktijd."""
    return d.weekday() >= 4


def lange_datum(d: date) -> str:
    """"woensdag 11 november": weekdag en maand uitgeschreven, zonder jaar."""
    return f"{WEEKDAGEN[d.weekday()]} {d.day} {MAANDEN[d.month - 1]}"


def bestelling_zin(start: date) -> str:
    """De regel boven het menu, in de app, de melding en op sensor.weekmenu."""
    return f"Voor de bestelling van {lange_datum(start)}"


def eat_days(start: date, away: Iterable[str] = ()) -> list[date]:
    """The eat days of the menu week: Wednesday to Tuesday, without the days in `away`."""
    away = set(away)
    days = (start + timedelta(days=i) for i in range(7))
    return [d for d in days if WEEKDAGEN[d.weekday()] not in away]


def plan_days(eat: list[date], kookmomenten: int) -> list[tuple[date, date | None]]:
    """(cook day, leftover night or None) for each cooking moment.

    The number of cooking moments is bounded to 1 .. len(eat). The extra eat days
    (len(eat) - k) become leftover nights: the earliest cooking days get the next eat
    day as their leftover night, at most one each. With fewer than half as many
    cooking moments as eat days, the last eat days get nothing.
    """
    eat = sorted(eat)
    if not eat:
        return []
    k = min(len(eat), max(1, kookmomenten))
    extra = len(eat) - k
    out: list[tuple[date, date | None]] = []
    i = 0
    while i < len(eat) and len(out) < k:
        if extra > 0 and i + 1 < len(eat):
            out.append((eat[i], eat[i + 1]))
            extra -= 1
            i += 2
        else:
            out.append((eat[i], None))
            i += 1
    return out
