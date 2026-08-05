#!/usr/bin/env python
"""Regenerate the README screenshots from demo data.

    uv run --group dev python scripts/screenshot.py

Always uses `--demo` data, so no real account usage is ever committed.

Two artefacts per theme:

* `.svg` — the vector source. Rich positions every run with an explicit `x`
  and `textLength`, so alignment survives whatever font the viewer falls back
  to. The upstream export links a webfont from a CDN; that is stripped here so
  a committed asset never points at a third party.
* `.png` — what the README actually embeds. Rasterising removes the last font
  dependency: block and box-drawing glyphs (█ ░ ╭ ◆) are baked in, so nobody
  sees tofu because their system monospace lacks coverage.
"""

from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from agents_fuel_gauge.app import FuelGaugeApp  # noqa: E402
from agents_fuel_gauge.demo import fetch_demo  # noqa: E402

OUT = REPO / "docs"
SIZE = (96, 20)

# Verified to cover U+2588 U+2591 U+256D U+25C6 on a stock Linux font set.
RASTER_FONT = "DejaVu Sans Mono"


def strip_remote_font(svg: str) -> str:
    """Drop the CDN `url(...)` source, keeping `local(...)` and the fallback."""
    svg = re.sub(r",\s*\n?\s*url\([^)]*\)\s*format\([^)]*\)", "", svg)
    return re.sub(r"url\(['\"]?https?://[^)]*\)", "", svg)


async def shoot(theme: str, stem: str) -> Path:
    app = FuelGaugeApp(interval=3600, fetcher=fetch_demo)
    async with app.run_test(size=SIZE) as pilot:
        app.theme = theme
        await pilot.pause()
        await pilot.pause()
        svg = app.export_screenshot(title="agents-fuel-gauge")

    path = OUT / f"{stem}.svg"
    path.write_text(strip_remote_font(svg))
    print(f"wrote {path.relative_to(REPO)}")
    return path


def rasterise(svg_path: Path) -> None:
    try:
        import cairosvg
    except ImportError:
        print(f"  (skipping {svg_path.stem}.png — cairosvg not installed)")
        return

    svg = svg_path.read_text().replace("Fira Code", RASTER_FONT)
    png = svg_path.with_suffix(".png")
    cairosvg.svg2png(bytestring=svg.encode(), write_to=str(png), scale=2.0)
    print(f"wrote {png.relative_to(REPO)}")


async def main() -> None:
    OUT.mkdir(exist_ok=True)
    for theme, stem in (("textual-dark", "screenshot-dark"),
                        ("textual-light", "screenshot-light")):
        rasterise(await shoot(theme, stem))


if __name__ == "__main__":
    asyncio.run(main())
