"""SVG -> greyscale bitmap via macOS Quick Look, shared by the icon build scripts.

macOS only. Used once on the development Mac; the resulting PNGs are committed,
so the server never renders SVG (and needs no SVG library).
"""

from __future__ import annotations

import re
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image

# Always render at this size and downscale afterwards: Quick Look draws
# smaller thumbnails at the wrong scale (content at about half size).
# (ImageMagick's built-in SVG renderer is no alternative: it drops strokes.)
RENDER_PX = 304


def require_macos() -> None:
    if sys.platform != "darwin":
        raise SystemExit("needs macOS (renders SVG with Quick Look)")


def sized(svg: str, px: int = RENDER_PX) -> str:
    """Give the root <svg> an explicit pixel size, replacing any it has."""
    def root(m: re.Match) -> str:
        tag = re.sub(r'\s(width|height)="[^"]*"', "", m.group(0))
        return tag.replace("<svg", f'<svg width="{px}px" height="{px}px"', 1)
    return re.sub(r"<svg\b[^>]*>", root, svg, count=1)


def quicklook(svgs: dict[str, str]) -> dict[str, Image.Image]:
    """Render {name: svg text} (already sized to RENDER_PX) to greyscale on white."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        svg_dir, png_dir = tmp_dir / "svg", tmp_dir / "png"
        svg_dir.mkdir()
        png_dir.mkdir()
        for name, svg in svgs.items():
            (svg_dir / f"{name}.svg").write_text(svg, encoding="utf-8")
        subprocess.run(
            ["qlmanage", "-t", "-s", str(RENDER_PX), "-o", str(png_dir),
             *sorted(str(p) for p in svg_dir.glob("*.svg"))],
            check=True, capture_output=True,
        )
        out = {}
        for name in svgs:
            im = Image.open(png_dir / f"{name}.svg.png").convert("RGBA")
            white = Image.new("RGBA", im.size, "white")
            white.alpha_composite(im)
            out[name] = white.convert("L")
        return out


def ink_union(images) -> tuple[int, int, int, int]:
    """Bounding box of all non-white pixels across the images."""
    boxes = [im.point(lambda v: 255 if v < 250 else 0).getbbox() for im in images]
    boxes = [b for b in boxes if b]
    return (min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[2] for b in boxes), max(b[3] for b in boxes))


def square(box: tuple[int, int, int, int], limit: int = RENDER_PX, pad: int = 4) -> tuple[int, int, int, int]:
    """The smallest square (plus padding) around box, kept inside 0..limit."""
    x0, y0, x1, y1 = box
    side = max(x1 - x0, y1 - y0) + 2 * pad
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    left = round(min(max(cx - side / 2, 0), limit - side))
    top = round(min(max(cy - side / 2, 0), limit - side))
    return left, top, left + side, top + side
