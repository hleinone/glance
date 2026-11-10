"""User-visible strings and date/time formatting for English and Finnish."""

from __future__ import annotations

from datetime import datetime

WEEKDAYS = {
    "en": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"],
    "fi": ["maanantai", "tiistai", "keskiviikko", "torstai", "perjantai", "lauantai", "sunnuntai"],
}
MONTHS_EN = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]

STRINGS = {
    "en": {
        "weather_old": "Weather {age} old",
        "feels_like": "Feels like {temp}",
        "battery_low": "Charge battery",
        "now": "now",
        "min": "min",
        "no_departures": "No departures",
        "weather_unavailable": "Weather unavailable",
        "departures_old": "Departures {age} old",
        "departures_unavailable": "Departures unavailable",
    },
    "fi": {
        "weather_old": "Sää {age} vanha",
        "feels_like": "Tuntuu kuin {temp}",
        "battery_low": "Lataa akku",
        "now": "nyt",
        "min": "min",
        "no_departures": "Ei lähtöjä",
        "weather_unavailable": "Säätietoja ei saatavilla",
        "departures_old": "Lähdöt {age} vanhoja",
        "departures_unavailable": "Lähtötietoja ei saatavilla",
    },
}


def t(language: str, key: str, **kwargs: str) -> str:
    return STRINGS[language][key].format(**kwargs)


def clock(language: str, when: datetime) -> str:
    sep = "." if language == "fi" else ":"
    return f"{when.hour:02d}{sep}{when.minute:02d}"


def date(language: str, when: datetime) -> str:
    weekday = WEEKDAYS[language][when.weekday()]
    if language == "fi":
        return f"{weekday.capitalize()} {when.day}.{when.month}."
    return f"{weekday} {when.day} {MONTHS_EN[when.month - 1]}"


def percent(language: str, value: int) -> str:
    """'87%' in English, '87 %' in Finnish (with a narrow no-break space)."""
    return f"{value}\u202f%" if language == "fi" else f"{value}%"


def age(minutes: float) -> str:
    minutes = int(minutes)
    if minutes < 60:
        return f"{minutes} min"
    hours, rest = divmod(minutes, 60)
    if hours >= 24:
        return f"{hours // 24} d"
    return f"{hours} h {rest} min" if rest and hours < 3 else f"{hours} h"
