"""Checking a value from the config files where it is read, with a message that
says what to change. Used by the config itself and by each source reading its
own section; it imports nothing of ours."""

from __future__ import annotations


def within(value, low: float, high: float, name: str, number=int):
    try:
        # bool is a number to Python: `port = true` would otherwise be port 1.
        value = number("" if isinstance(value, bool) else value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a number, not {value!r}") from None
    if not low <= value <= high:
        raise ValueError(f"{name} must be {low}..{high}, not {value}")
    return value
