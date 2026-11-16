"""English and Finnish: formats for dates, times, ages and percentages, and
looking up a part's own words. Each part that shows words keeps them itself,
as WORDS = {"en": {...}, "fi": {...}}: a widget, a source, the status bar."""

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

def t(words: dict[str, dict[str, str]], language: str, key: str, **kwargs: str) -> str:
    return words[language][key].format(**kwargs)


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
