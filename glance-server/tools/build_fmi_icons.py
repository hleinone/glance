"""Build the weather icons from FMI's WeatherSymbol3 SVGs, adapted for 4-grey e-paper.

    .venv/bin/python tools/build_fmi_icons.py

Runs on macOS only (SVG rendering via Quick Look, `qlmanage`), once, on the
development Mac; the resulting PNGs are committed, so the server (a Pi 1) never
renders SVG. Source: https://github.com/fmidev/opendata-resources (MIT licence,
see icons/fmi/LICENSE). The original SVGs, stripped of Adobe Illustrator's
private data, are kept in icons/fmi/src/.

Adaptations, because the originals are coloured and drawn with hairlines:
- thicker cloud outlines (more so for the small size)
- sun: light grey with a thin black outline, so it doesn't fade into white
- lightning: black (FMI draws it in the sun's yellow; told apart by drawing
  order: the sun comes before the cloud, lightning after it)
- snowflakes: black and thickened; raindrops: dark grey, black when small (told apart by
  path length: a drop is a short teardrop, a flake a long jagged outline)
"""

from __future__ import annotations

import re
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

import svg_render

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "icons" / "fmi"
SRC = OUT / "src"
# Pinned to a commit, not the moving master branch: a missing source is
# fetched again as the file the committed ones came from (checked identical,
# after the stripping below, 2026-11-13).
BASE_URL = ("https://raw.githubusercontent.com/fmidev/opendata-resources/"
            "18f6074ebedf2f964295195f439940e6963fab8d/symbols/WeatherSymbol3")
CODES = [1, 2, 3, 21, 22, 23, 31, 32, 33, 41, 42, 43, 51, 52, 53,
         61, 62, 63, 64, 71, 72, 73, 81, 82, 83, 91, 92]

YELLOW, BLUE = "#FABA25", "#009ED6"
SUN, LIGHTNING = "#AAAAAA", "#000000"
FLAKE = "#000000"


@dataclass(frozen=True)
class Style:
    size: int  # output px
    cloud: float  # outline width, in SVG units (the drawing is 37 units wide)
    sun: float
    flake: float
    drop: str  # raindrop colour


STYLES = [
    Style(size=76, cloud=1.5, sun=0.5, flake=0.45, drop="#555555"),
    # Small: a dark-grey drop is only a few pixels, so make drops black.
    Style(size=36, cloud=2.3, sun=0.8, flake=0.8, drop="#000000"),
]


def fetch_sources() -> None:
    SRC.mkdir(parents=True, exist_ok=True)
    for code in CODES:
        path = SRC / f"{code}.svg"
        if path.exists():
            continue
        with urllib.request.urlopen(f"{BASE_URL}/{code}.svg", timeout=30) as r:
            svg = r.read().decode("utf-8")
        # Drop Illustrator's embedded private data (~130 KB of base64 per file).
        svg = re.sub(r"<switch>\s*<foreignObject.*?</foreignObject>", "<switch>", svg, flags=re.S)
        svg = re.sub(r"<i:pgf\b.*?</i:pgf>", "", svg, flags=re.S)
        path.write_text(svg, encoding="utf-8")


def _set(attrs: str, name: str, value: str) -> str:
    if re.search(rf'\b{name}="[^"]*"', attrs):
        return re.sub(rf'\b{name}="[^"]*"', f'{name}="{value}"', attrs)
    return f'{attrs} {name}="{value}"'


def adapt(svg: str, style: Style) -> str:
    seen_cloud = False

    def element(m: re.Match) -> str:
        nonlocal seen_cloud
        tag, attrs, close = m.group(1), m.group(2), m.group(3)
        fill = (re.search(r'\bfill="([^"]*)"', attrs) or [None, None])[1]
        stroke = (re.search(r'\bstroke="([^"]*)"', attrs) or [None, None])[1]
        geometry = (re.search(r'\b(?:d|points)="([^"]*)"', attrs) or [None, ""])[1]
        if fill == "#FFFFFF" and stroke == "#000000":  # the cloud
            seen_cloud = True
            attrs = _set(attrs, "stroke-width", f"{style.cloud}")
            attrs = _set(attrs, "stroke-linejoin", "round")
        elif fill == YELLOW and not seen_cloud:  # sun (rays and disc)
            attrs = _set(attrs, "fill", SUN)
            if stroke is None:  # the rays; the disc keeps its white gap ring
                attrs = _set(attrs, "stroke", "#000000")
                attrs = _set(attrs, "stroke-width", f"{style.sun}")
                attrs = _set(attrs, "stroke-linejoin", "round")
        elif fill == YELLOW:  # lightning
            attrs = _set(attrs, "fill", LIGHTNING)
        elif fill == BLUE and len(geometry) > 500:  # snowflake
            attrs = _set(attrs, "fill", FLAKE)
            attrs = _set(attrs, "stroke", FLAKE)
            attrs = _set(attrs, "stroke-width", f"{style.flake}")
            attrs = _set(attrs, "stroke-linejoin", "round")
        elif fill == BLUE:  # raindrop
            attrs = _set(attrs, "fill", style.drop)
        return f"<{tag}{attrs}{close}"

    svg = re.sub(r"<(path|polygon|circle)\b([^>]*?)(/?>)", element, svg)
    # Render big, with a little margin so thicker outlines aren't clipped.
    svg = svg_render.sized(svg)
    return re.sub(r'viewBox="0 0 37 37"', 'viewBox="-1 -1 39 39"', svg, count=1)


def render_all() -> None:
    for style in STYLES:
        rendered = svg_render.quicklook(
            {str(code): adapt((SRC / f"{code}.svg").read_text(encoding="utf-8"), style) for code in CODES}
        )
        # FMI's drawings leave wide margins; crop every icon to the same
        # square around the union of all their ink, so they fill the space
        # like the rest of the screen but keep their relative sizes.
        box = svg_render.square(svg_render.ink_union(rendered.values()))
        out_dir = OUT / str(style.size)
        out_dir.mkdir(parents=True, exist_ok=True)
        for code, im in rendered.items():
            icon = im.crop(box).resize((style.size, style.size), Image.Resampling.LANCZOS)
            icon.save(out_dir / f"{code}.png", optimize=True)


def main() -> None:
    svg_render.require_macos()
    fetch_sources()
    render_all()
    print(f"wrote {len(CODES) * len(STYLES)} icons to {OUT.relative_to(ROOT)}/")


if __name__ == "__main__":
    main()
