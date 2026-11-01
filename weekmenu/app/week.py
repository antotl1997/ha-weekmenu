"""Menuweek en kookavonden. Pure logica, geen I/O."""

from datetime import date, datetime, timedelta

WEEKDAGEN = ["maandag", "dinsdag", "woensdag", "donderdag", "vrijdag", "zaterdag", "zondag"]

GEEN_OUDERAVOND = "Niet deze week"

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


def evenings(start: date, aantal: int, ouders: str) -> list[date]:
    """Kookavonden vanaf `start`, woensdag t/m dinsdag (start + 6).

    De ouderavond (een weekdag, of GEEN_OUDERAVOND) wordt overgeslagen.
    Geeft minder dagen terug als de week er niet genoeg heeft.
    """
    if ouders != GEEN_OUDERAVOND and ouders not in WEEKDAGEN:
        raise ValueError(f"onbekende ouderavond: {ouders!r}")
    if not 1 <= aantal <= 7:
        raise ValueError(f"aantal avonden moet tussen 1 en 7 liggen, niet {aantal}")

    result: list[date] = []
    for offset in range(7):
        if len(result) == aantal:
            break
        day = start + timedelta(days=offset)
        if ouders != GEEN_OUDERAVOND and WEEKDAGEN[day.weekday()] == ouders:
            continue
        result.append(day)
    return result


def is_weekend(d: date) -> bool:
    """Vrijdag, zaterdag of zondag: geen tijdgrens voor de kooktijd."""
    return d.weekday() >= 4
