"""Build the transport mode icons from HSL's own pictograms.

    .venv/bin/python tools/build_hsl_icons.py

macOS only (renders SVG with Quick Look), run once on the development Mac; the
PNGs are committed. Source: HSL's journey planner, digitransit-ui
(https://github.com/HSLdevcom/digitransit-ui, app/assets/icons/default/),
dual-licensed EUPL 1.2 / AGPLv3: its COPYING and LICENSE-EUPL.txt are copied
into icons/hsl/ and must stay with the icons.

The pictograms are single-colour (a rounded square with a white vehicle), so
they suit e-paper as they are: drawn black, cropped to the square, scaled down.
"""

from __future__ import annotations

import urllib.request
from pathlib import Path

from PIL import Image

import svg_render

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "icons" / "hsl"
SRC = OUT / "src"
# Pinned to a commit, not the moving v3 branch: a missing source is fetched
# again as the file the committed ones came from (checked identical 2026-11-13).
REPO = "https://raw.githubusercontent.com/HSLdevcom/digitransit-ui/3f151f9dce37ede2a3bc1df45865b82f398ffcf4"
# Our mode name (Digitransit's vehicleMode, lower-cased) -> HSL's icon file.
MODES = {"tram": "icon_tram", "bus": "icon_bus", "subway": "icon_subway", "rail": "icon_rail", "ferry": "icon_ferry"}
SIZES = [28]


def _get(url: str) -> str:
    with urllib.request.urlopen(url, timeout=30) as r:
        return r.read().decode("utf-8")


def fetch_sources() -> None:
    SRC.mkdir(parents=True, exist_ok=True)
    for mode, name in MODES.items():
        path = SRC / f"{mode}.svg"
        if not path.exists():
            path.write_text(_get(f"{REPO}/app/assets/icons/default/{name}.svg"), encoding="utf-8")
    for name in ("COPYING", "LICENSE-EUPL.txt"):
        path = OUT / name
        if not path.exists():
            path.write_text(_get(f"{REPO}/{name}"), encoding="utf-8")
    readme = OUT / "README"
    if not readme.exists():
        readme.write_text(
            "Transport mode pictograms from HSL's journey planner, digitransit-ui:\n"
            "https://github.com/HSLdevcom/digitransit-ui (app/assets/icons/default/).\n"
            "Copyright 2015 Finnish Transport Agency and Helsinki Regional Transport\n"
            "Authority HSL; dual-licensed under the EUPL v1.2 and AGPLv3 (see COPYING\n"
            "and LICENSE-EUPL.txt). src/ holds the original SVGs; the PNGs are\n"
            "rendered from them unchanged except for cropping and scaling, by\n"
            "tools/build_hsl_icons.py.\n",
            encoding="utf-8",
        )


def render_all() -> None:
    rendered = svg_render.quicklook(
        {mode: svg_render.sized((SRC / f"{mode}.svg").read_text(encoding="utf-8")) for mode in MODES}
    )
    for size in SIZES:
        out_dir = OUT / str(size)
        out_dir.mkdir(parents=True, exist_ok=True)
        for mode, im in rendered.items():
            # Each pictogram fills its own square; crop to it (their viewBoxes differ).
            box = svg_render.square(svg_render.ink_union([im]), pad=0)
            im.crop(box).resize((size, size), Image.Resampling.LANCZOS).save(out_dir / f"{mode}.png", optimize=True)


def main() -> None:
    svg_render.require_macos()
    fetch_sources()
    render_all()
    print(f"wrote {len(MODES) * len(SIZES)} icons to {OUT.relative_to(ROOT)}/")


if __name__ == "__main__":
    main()
