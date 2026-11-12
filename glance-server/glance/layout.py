"""Screen layout: a grid picture of widget names, like CSS grid-template-areas.

    weather weather hours  hours  hours  hours
    trams   trams   buses  buses  trains trains
    trams   trams   buses  buses  trains trains

Words per line give the columns, lines give the rows, and each name must fill
a rectangle. "." leaves a cell empty. The grid size is free: cells are an
equal share of the space below the status bar (800×450 px).
"""

from __future__ import annotations

from dataclasses import dataclass

EMPTY = "."

W, H = 800, 480  # the panel
BAR = 30  # status bar height; the grid gets the rest

Rect = tuple[int, int, int, int]  # x0, y0, x1, y1 (exclusive)


@dataclass(frozen=True)
class Area:
    name: str
    col: int
    row: int
    cols: int
    rows: int


@dataclass(frozen=True)
class Layout:
    cols: int
    rows: int
    areas: tuple[Area, ...]  # in reading order of each name's top-left cell

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(a.name for a in self.areas)

    def x(self, col: int) -> int:
        return round(col * W / self.cols)

    def y(self, row: int) -> int:
        return BAR + round(row * (H - BAR) / self.rows)

    def rect(self, a: Area) -> Rect:
        """An area's pixels: cells share the space below the status bar equally."""
        return self.x(a.col), self.y(a.row), self.x(a.col + a.cols), self.y(a.row + a.rows)


def parse(grid: str) -> Layout:
    lines = [line.split() for line in grid.strip().splitlines() if line.strip()]
    if not lines:
        raise ValueError("layout grid is empty")
    cols = len(lines[0])
    for i, line in enumerate(lines):
        if len(line) != cols:
            raise ValueError(
                f"layout grid: line {i + 1} has {len(line)} cells, the first line has {cols}"
            )
    areas = []
    seen: set[str] = set()
    for row, line in enumerate(lines):
        for col, name in enumerate(line):
            if name == EMPTY or name in seen:
                continue
            seen.add(name)
            cells = [(r, c) for r, ln in enumerate(lines) for c, n in enumerate(ln) if n == name]
            r0, r1 = min(r for r, _ in cells), max(r for r, _ in cells)
            c0, c1 = min(c for _, c in cells), max(c for _, c in cells)
            if len(cells) != (r1 - r0 + 1) * (c1 - c0 + 1):
                raise ValueError(f"layout grid: {name!r} doesn't fill a rectangle")
            areas.append(Area(name, c0, r0, c1 - c0 + 1, r1 - r0 + 1))
    return Layout(cols, len(lines), tuple(areas))
