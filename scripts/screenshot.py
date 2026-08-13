#!/usr/bin/env python
"""Regenerate the README screenshots from demo data.

    uv run --group dev python scripts/screenshot.py

Always uses `--demo` data and generated history, so no real account usage is
ever committed.

Two artefacts per theme:

* `.svg` — the vector source. Rich positions every run with an explicit `x`
  and `textLength`, so alignment survives whatever font the viewer falls back
  to. The upstream export links a webfont from a CDN; that is stripped here so
  a committed asset never points at a third party.
* `.png` — what the README actually embeds. Rasterising removes the last font
  dependency: block, box-drawing and arrow glyphs (█ ░ ╭ ↓ ↑) are baked in, so nobody
  sees tofu because their system monospace lacks coverage.
"""

from __future__ import annotations

import asyncio
import math
import os
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from agents_fuel_gauge import history  # noqa: E402
from agents_fuel_gauge.app import FuelGaugeApp  # noqa: E402
from agents_fuel_gauge.demo import demo_snapshots, fetch_demo  # noqa: E402

OUT = REPO / "docs"
# Tall enough that the panels never need to scroll: a scrollbar in a still
# image reads as content the reader is missing rather than as a live control.
SIZE = (96, 22)
HISTORY_SIZE = (96, 34)
SEGMENTS_SIZE = (96, 76)

# Verified to cover U+2588 U+2591 U+256D U+2193 U+2191 on a stock Linux font set.
RASTER_FONT = "DejaVu Sans Mono"
STYLE_COLOR = re.compile(r"-r\d+\s*\{\s*fill:\s*#([0-9a-fA-F]{6})")


def strip_remote_font(svg: str) -> str:
    """Drop the CDN `url(...)` source, keeping `local(...)` and the fallback."""
    svg = re.sub(r",\s*\n?\s*url\([^)]*\)\s*format\([^)]*\)", "", svg)
    svg = re.sub(r"url\(['\"]?https?://[^)]*\)", "", svg)
    return "\n".join(line.rstrip() for line in svg.splitlines())


def require_semantic_colors(svg: str) -> None:
    """Fail generation if Textual flattened every content color to gray."""
    chromatic = 0
    for encoded in STYLE_COLOR.findall(svg):
        red, green, blue = (
            int(encoded[index : index + 2], 16) for index in (0, 2, 4)
        )
        if max(red, green, blue) - min(red, green, blue) >= 32:
            chromatic += 1
    if chromatic < 3:
        raise RuntimeError(
            "screenshot lost its semantic colors; refusing to overwrite docs"
        )


def synthetic_series(
    end: float,
    end_percent: float,
    sample_seconds: int = 30 * 60,
) -> list[history.Sample]:
    """Build five alternating linear/variable portions for documentation."""
    total_seconds = int(4.5 * 86_400)
    accrued = [0.0]
    previous_rate = 18.0
    for elapsed in range(0, int(total_seconds) + 1, sample_seconds):
        day = elapsed / 86_400
        if day < 1:
            rate = 18.0
        elif day < 2.25:
            # Two rate waves produce one cumulative roller-coaster portion.
            rate = 14 + 10 * math.sin(4 * math.pi * (day - 1) / 1.25)
        elif day < 3.25:
            rate = 4.0
        elif day < 4:
            # A single acceleration/deceleration arc is visibly different.
            rate = 4 + 28 * math.sin(math.pi * (day - 3.25) / 0.75) ** 2
        else:
            rate = 0.0
        if elapsed:
            accrued.append(
                accrued[-1]
                + (previous_rate + rate) / 2 * sample_seconds / 86_400
            )
        previous_rate = rate

    base_percent = end_percent - accrued[-1]
    return [
        {
            "t": end - total_seconds + index * sample_seconds,
            "pct": float(int(base_percent + used)),
        }
        for index, used in enumerate(accrued)
    ]


def synthetic_history():
    """History shaped for documentation, keyed like the production journal."""
    snapshots = demo_snapshots()
    generated = {
        ("claude", gauge.label): synthetic_series(
            snapshots[0].captured_at.timestamp(),
            gauge.percent,
        )
        for gauge in snapshots[0].gauges
    }
    portions = history.episodes(generated[("claude", "7d Fable")])
    if [portion.linear for portion in portions] != [
        True,
        False,
        True,
        False,
        True,
    ]:
        raise RuntimeError(
            "synthetic history no longer demonstrates five alternating portions"
        )
    return snapshots, generated


async def shoot(
    theme: str,
    stem: str,
    *,
    view: str = "dashboard",
    size: tuple[int, int] = SIZE,
) -> Path:
    snapshots, generated = synthetic_history()

    async def fetch_synthetic():
        # Every history screenshot follows this same provider and sample set,
        # so Recorded range, Full range, and Segments are directly comparable.
        return snapshots[:1]

    original_read_window = history.read_window
    original_no_color = os.environ.pop("NO_COLOR", None)
    try:
        history.read_window = lambda provider, label, *_: generated.get(
            (provider, label), []
        )
        # Documentation screenshots are a visual product. Do not let an
        # agent, CI runner, or monochrome shell silently bleach their semantic
        # severity colors through the conventional NO_COLOR environment flag.
        app = FuelGaugeApp(
            interval=3600,
            fetcher=fetch_demo if view == "dashboard" else fetch_synthetic,
        )
        async with app.run_test(size=size) as pilot:
            app.theme = theme
            await pilot.pause()
            await pilot.pause()
            if view != "dashboard":
                await pilot.press("h")
                await pilot.pause()
            if view == "full":
                await pilot.press("z")
                await pilot.pause()
            elif view == "segments":
                await pilot.press("d")
                await pilot.pause()
            svg = app.export_screenshot(title="agents-fuel-gauge")
            require_semantic_colors(svg)
    finally:
        history.read_window = original_read_window
        if original_no_color is not None:
            os.environ["NO_COLOR"] = original_no_color

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
    for stem, view, size in (
        ("history-recorded-dark", "recorded", HISTORY_SIZE),
        ("history-full-dark", "full", HISTORY_SIZE),
        ("history-details-dark", "segments", SEGMENTS_SIZE),
    ):
        rasterise(
            await shoot(
                "textual-dark",
                stem,
                view=view,
                size=size,
            )
        )


if __name__ == "__main__":
    asyncio.run(main())
