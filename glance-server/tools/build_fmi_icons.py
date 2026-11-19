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

Each symbol with a sun also gets a night version (`<code>-night.png`): the sun's rays
removed and its disc replaced by a crescent moon, drawn as the sun is. FMI's own night
symbols (SmartSymbol) draw the moon as a plain disc, which on four greys reads as a sun
without its rays.

It also makes the sunrise and sunset widget's icons (icons/fmi/horizon/): symbol 1's
sun cut at the horizon, with a horizon and a line of water; the rising disc light grey,
the setting one black, and each also as a light grey line drawing for a time passed.
"""

from __future__ import annotations

import math
import re
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageOps

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
            if stroke is None:  # the rays, or the moon; the disc keeps its white gap ring
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


CLOUD = '<path fill="#FFFFFF" stroke="#000000"'


def crescent(cx: float, cy: float, r: float) -> str:
    """A crescent moon centred where a sun disc of radius r was: a disc a little larger,
    with a bite out of its upper right, filled as the sun is."""
    outer, inner = r * 1.5, r * 1.5 * 0.82
    bite = math.radians(-40)
    dx, dy = math.cos(bite) * outer * 0.62, math.sin(bite) * outer * 0.62
    d = math.hypot(dx, dy)
    # Where the two circles cross: the crescent's horns.
    along = (outer * outer - inner * inner + d * d) / (2 * d)
    across = math.sqrt(outer * outer - along * along)
    mx, my = cx + along * dx / d, cy + along * dy / d
    (px, py), (qx, qy) = (mx + across * dy / d, my - across * dx / d), (mx - across * dy / d, my + across * dx / d)
    return (f'<path fill="{YELLOW}" d="M{px:.3f},{py:.3f} A{outer:.3f},{outer:.3f} 0 1,0 {qx:.3f},{qy:.3f} '
            f'A{inner:.3f},{inner:.3f} 0 0,1 {px:.3f},{py:.3f} Z"/>')


def night(svg: str) -> str | None:
    """The symbol at night: its sun (the rays and the disc, drawn before the cloud) replaced
    by a crescent; None for a symbol without a sun."""
    sky = svg[: svg.find(CLOUD)] if CLOUD in svg else svg
    rays = re.search(rf'<polygon fill="{YELLOW}"[^>]*/>', sky)
    disc = re.search(rf'<circle fill="{YELLOW}"[^>]*\bcx="([\d.]+)" cy="([\d.]+)" r="([\d.]+)"/>', sky)
    if not rays or not disc:
        return None
    return svg.replace(rays.group(0), "", 1).replace(disc.group(0), crescent(*map(float, disc.groups())), 1)


def symbols() -> dict[str, str]:
    """Each symbol's source by its file name: the code, and `<code>-night` for one with a sun."""
    out = {}
    for code in CODES:
        svg = (SRC / f"{code}.svg").read_text(encoding="utf-8")
        out[str(code)] = svg
        if (moon := night(svg)) is not None:
            out[f"{code}-night"] = moon
    return out


def render_all() -> None:
    sources = symbols()
    for style in STYLES:
        rendered = svg_render.quicklook({name: adapt(svg, style) for name, svg in sources.items()})
        # FMI's drawings leave wide margins; crop every icon to the same
        # square around the union of the day symbols' ink, so they fill the
        # space like the rest of the screen but keep their relative sizes.
        # The moon sits inside the sun's rays, so the night ones fit the same
        # square, and adding them moves no day symbol.
        box = svg_render.square(svg_render.ink_union(rendered[str(code)] for code in CODES))
        out_dir = OUT / str(style.size)
        out_dir.mkdir(parents=True, exist_ok=True)
        for name, im in rendered.items():
            icon = im.crop(box).resize((style.size, style.size), Image.Resampling.LANCZOS)
            icon.save(out_dir / f"{name}.png", optimize=True)


HORIZON_OUT = OUT / "horizon"
HORIZON_SIZE = (42, 28)  # px: one row of the sunrise and sunset widget
HORIZON_Y = 18.363  # the sun's centre in symbol 1: the horizon cuts it in half
PAST = "#AAAAAA"


def horizon_svg(rising: bool, past: bool) -> str:
    """FMI's sun (symbol 1) cut at the horizon, over a horizon line and a line
    of water. Rising: the disc light grey, as in the weather icons; setting:
    black. Past: the same drawing in light grey lines, the rising disc white."""
    sun = (SRC / "1.svg").read_text(encoding="utf-8")
    polygon = re.search(r"<polygon\b[^>]*/>", sun).group(0)
    circle = re.search(r"<circle\b[^>]*/>", sun).group(0)
    line = PAST if past else "#000000"
    rays = _set(_set(_set(polygon[len("<polygon"):-2], "fill", "#FFFFFF" if past else SUN), "stroke", line), "stroke-width", "0.7")
    rays = _set(rays, "stroke-linejoin", "round")
    disc_fill = (PAST if not rising else "#FFFFFF") if past else (SUN if rising else "#000000")
    disc = _set(circle[len("<circle"):-2], "fill", disc_fill)
    if past and rising:  # a white disc needs an outline to be seen
        disc = _set(_set(disc, "stroke", PAST), "stroke-width", "0.7")
    return svg_render.sized(
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="4.5 1 28 28">'
        f'<defs><clipPath id="sky"><rect x="0" y="0" width="37" height="{HORIZON_Y}"/></clipPath></defs>'
        f'<g clip-path="url(#sky)"><polygon{rays}/><circle{disc}/></g>'
        f'<line x1="6" y1="{HORIZON_Y}" x2="31" y2="{HORIZON_Y}" stroke="{line}" stroke-width="1.6" stroke-linecap="round"/>'
        f'<line x1="11.5" y1="{HORIZON_Y + 3.2}" x2="25.5" y2="{HORIZON_Y + 3.2}" stroke="{line}" stroke-width="1.3" stroke-linecap="round"/>'
        "</svg>"
    )


def render_horizon() -> None:
    names = {f"{'sunrise' if rising else 'sunset'}{'-past' if past else ''}": (rising, past)
             for rising in (True, False) for past in (False, True)}
    rendered = svg_render.quicklook({name: horizon_svg(*spec) for name, spec in names.items()})
    # One box for all four, around their ink, widened to the output's shape.
    x0, y0, x1, y1 = svg_render.ink_union(rendered.values())
    w, h = HORIZON_SIZE
    width = max(x1 - x0, (y1 - y0) * w / h) + 20
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    # The box can reach past the rendered square: crop from it inside a white margin,
    # since a crop past the edge comes out black.
    margin = svg_render.RENDER_PX
    box = (round(cx - width / 2) + margin, round(cy - width * h / w / 2) + margin,
           round(cx + width / 2) + margin, round(cy + width * h / w / 2) + margin)
    HORIZON_OUT.mkdir(parents=True, exist_ok=True)
    for name, im in rendered.items():
        icon = ImageOps.expand(im, border=margin, fill=255).crop(box)
        icon.resize(HORIZON_SIZE, Image.Resampling.LANCZOS).save(HORIZON_OUT / f"{name}.png", optimize=True)


def main() -> None:
    svg_render.require_macos()
    fetch_sources()
    render_all()
    render_horizon()
    count = len(symbols()) * len(STYLES)
    print(f"wrote {count} icons to {OUT.relative_to(ROOT)}/, and 4 to {HORIZON_OUT.relative_to(ROOT)}/")


if __name__ == "__main__":
    main()
