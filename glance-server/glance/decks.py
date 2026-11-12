"""Decks of screens, and which screen is showing.

A deck is an ordered list of screens with a starting screen. The schedule
picks the deck by time of day; the device's buttons move through it. The
selection stays across refreshes and wakes, and goes back to the deck's
starting screen only when the schedule switches to another deck. It lives in
the server's memory: a restart starts the deck over.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass


@dataclass(frozen=True)
class Deck:
    name: str
    screens: tuple[str, ...]
    start: str

    def neighbours(self, screen: str) -> tuple[str, ...]:
        """The screens a button press goes to from `screen` (previous, next)."""
        i, n = self.screens.index(screen), len(self.screens)
        return tuple(dict.fromkeys(s for s in (self.screens[(i - 1) % n], self.screens[(i + 1) % n]) if s != screen))


class Selection:
    """The showing screen of the deck last shown. Shared by the device and the
    browser preview (the preview behaves like the device)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._deck: str | None = None
        self._index = 0

    def _enter(self, deck: Deck) -> None:
        if self._deck != deck.name or self._index >= len(deck.screens):
            self._deck, self._index = deck.name, deck.screens.index(deck.start)

    def current(self, deck: Deck) -> tuple[str, int]:
        """(screen, its position in the deck) now that `deck` is showing; a
        switch from another deck starts at its starting screen."""
        with self._lock:
            self._enter(deck)
            return deck.screens[self._index], self._index

    def peek(self, deck: Deck) -> str:
        """The screen `deck` would show, without switching to it: for drawing
        ahead of a minute that may start another deck."""
        with self._lock:
            if self._deck == deck.name and self._index < len(deck.screens):
                return deck.screens[self._index]
            return deck.start

    def step(self, deck: Deck, delta: int) -> bool:
        """Move delta screens along `deck` (wrapping around); False if the
        deck has only one screen, so nothing changes."""
        with self._lock:
            self._enter(deck)
            if len(deck.screens) < 2:
                return False
            self._index = (self._index + delta) % len(deck.screens)
            return True
