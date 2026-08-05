"""Entry point.

Three ways to read the same data:

  afg                  live TUI
  afg --check          one-shot, plain text (default: greppable, no colour)
  afg --check --pretty one-shot, with bars and colour
  afg --json           one-shot, machine-readable
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from . import __version__
from .models import ProviderSnapshot, format_age, format_countdown
from .sources import fetch_all

ANSI = {"normal": "\033[32m", "warning": "\033[33m", "critical": "\033[31m"}
RESET = "\033[0m"
DIM = "\033[2m"
BOLD = "\033[1m"

# The vendor usage endpoints are themselves rate-limited, and polling them
# aggressively earns a 429 long before your real quota runs out.
MIN_INTERVAL = 15.0

BAR_WIDTH = 24


def render_plain(snapshots: list[ProviderSnapshot]) -> str:
    """One row per gauge, fixed columns, no escape codes.

    Built for `grep`/`awk` and for pasting into an issue, so the marker is the
    word FIRST rather than a symbol, and nothing depends on colour.
    """
    # Two row kinds: aligned data rows, and free-form error rows. They are
    # collected separately so column widths are computed only over rows that
    # actually have columns — an error-only result has none.
    rows: list[tuple[str, tuple[str, ...]]] = []
    for snap in snapshots:
        if snap.error:
            rows.append(("error", (snap.key, snap.error)))
        for gauge in snap.gauges:
            flags = []
            if gauge.runs_out_first:
                flags.append("FIRST")
            if snap.stale:
                flags.append("STALE")
            rows.append(
                (
                    "data",
                    (
                        snap.key,
                        gauge.window,
                        # Every field must be a single whitespace-free token,
                        # or `awk '$4+0 > 80'` silently reads the wrong column:
                        # "all models" and "2h 10m" would each split in two.
                        gauge.scope.replace(" ", "-"),
                        f"{gauge.percent:.0f}%",
                        format_countdown(gauge.seconds_remaining()).replace(" ", "")
                        or "-",
                        ",".join(flags) or "-",
                    ),
                )
            )
    if not rows:
        return "no usage data\n"

    data = [cells for kind, cells in rows if kind == "data"]
    widths = (
        [max(len(cells[i]) for cells in data) for i in range(6)] if data else [0] * 6
    )

    out = []
    for kind, cells in rows:
        if kind == "error":
            out.append(f"{cells[0].ljust(widths[0])}  ERROR  {cells[1]}")
        else:
            out.append(
                "  ".join(c.ljust(widths[i]) for i, c in enumerate(cells)).rstrip()
            )
    return "\n".join(out) + "\n"


def render_pretty(snapshots: list[ProviderSnapshot], color: bool) -> str:
    """The same data with bars — a compact echo of the TUI."""
    lines: list[str] = []
    for snap in snapshots:
        head = snap.display_name
        if snap.subtitle:
            head += f"  ({snap.subtitle})"
        head += f"  {format_age(snap.captured_at)}"
        lines.append("")
        lines.append(f"{BOLD if color else ''}{head}{RESET if color else ''}")

        if snap.error:
            prefix = "stale — " if snap.stale else ""
            lines.append(f"  !  {prefix}{snap.error}")
            if not snap.gauges:
                continue
        if not snap.gauges:
            lines.append("  no quota windows reported")
            continue

        for gauge in snap.gauges:
            filled = min(BAR_WIDTH, round(gauge.percent / 100 * BAR_WIDTH))
            bar = "█" * filled + "░" * (BAR_WIDTH - filled)
            tint, off = (ANSI.get(gauge.severity, ""), RESET) if color else ("", "")
            marker = "◆" if gauge.runs_out_first else " "
            countdown = format_countdown(gauge.seconds_remaining())
            lines.append(
                f"  {marker} {gauge.label:<24}{tint}{bar}{off} "
                f"{tint}{gauge.percent:>3.0f}%{off}  {countdown}"
            )
    if not lines:
        return "no usage data\n"
    lines.append("")
    lines.append(
        f"{DIM if color else ''}◆ runs out before the others{RESET if color else ''}"
    )
    return "\n".join(lines) + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="afg",
        description=(
            "Every quota bar for Claude Code and Codex in one view, including "
            "the per-model scoped caps that other tools drop."
        ),
        epilog=(
            "With no options this opens a live TUI. Use --check for a one-shot "
            "reading you can pipe or paste."
        ),
    )
    parser.add_argument("--version", action="version", version=f"afg {__version__}")
    parser.add_argument(
        "-c", "--check", "-1", "--once", action="store_true", dest="check",
        help="print one reading and exit, instead of opening the TUI",
    )
    parser.add_argument(
        "-p", "--pretty", action="store_true",
        help="with --check, draw bars and colour instead of plain columns",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="print one reading as JSON and exit (for scripts and status bars)",
    )
    parser.add_argument(
        "-i", "--interval", type=float, default=60.0,
        help=f"TUI refresh seconds (default: 60, minimum: {MIN_INTERVAL:.0f})",
    )
    parser.add_argument(
        "--no-color", action="store_true", help="never emit ANSI colour",
    )
    parser.add_argument(
        "--demo", action="store_true",
        help="render synthetic data instead of your accounts (no network)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.demo:
        from .demo import fetch_demo as fetcher
    else:
        fetcher = fetch_all

    if args.check or args.json or args.pretty:
        snapshots = asyncio.run(fetcher())
        if args.json:
            json.dump([s.to_dict() for s in snapshots], sys.stdout, indent=2)
            sys.stdout.write("\n")
        elif args.pretty:
            color = not args.no_color and sys.stdout.isatty()
            sys.stdout.write(render_pretty(snapshots, color))
        else:
            sys.stdout.write(render_plain(snapshots))
        # Non-zero when anything failed, so `afg --check` composes in scripts.
        return 0 if all(s.ok for s in snapshots) else 1

    from .app import FuelGaugeApp

    interval = max(MIN_INTERVAL, args.interval)
    if interval != args.interval:
        print(
            f"interval raised to {interval:.0f}s (the usage APIs rate-limit "
            f"faster polling)",
            file=sys.stderr,
        )
    FuelGaugeApp(interval=interval, fetcher=fetcher if args.demo else None).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
