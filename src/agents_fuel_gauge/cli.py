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
import time
from datetime import datetime, timezone

from . import BANNER, cache
from .models import (
    PACE_ARROW,
    ProviderSnapshot,
    directives,
    format_age,
    format_countdown,
    format_remaining,
    format_reset_at,
)
from .sources import fetch_all


ANSI = {"normal": "\033[32m", "warning": "\033[33m", "critical": "\033[31m"}
RESET = "\033[0m"
DIM = "\033[2m"
BOLD = "\033[1m"

# The vendor usage endpoints are themselves rate-limited, and polling them
# aggressively earns a 429 long before your real quota runs out.
MIN_INTERVAL = 15.0

BAR_WIDTH = 24

# The arrow points the way you should move, and is coloured to match: red to
# ease off, green for headroom. Same key the TUI prints under its panels.
PACE_ANSI = {
    "slow_down": "\033[31m",
    "exhausted": "\033[31m",
    "spare_capacity": "\033[32m",
    "on_track": "",
    "too_early": "",
}
LEGEND = (
    ("slow_down", "slow down"),
    ("spare_capacity", "speed up"),
    ("exhausted", "spent"),
)
LEGEND_NOTE = (
    "the % is how far to change that meter's average rate so far, "
    "so it lasts until it resets"
)


def _legend(color: bool) -> str:
    parts = []
    for verdict, meaning in LEGEND:
        tint = PACE_ANSI.get(verdict, "") if color else ""
        off = RESET if color and tint else ""
        parts.append(f"{tint}{PACE_ARROW[verdict]}{off} {meaning}")
    key = "   ".join(parts)
    return f"{key}\n{DIM if color else ''}{LEGEND_NOTE}{RESET if color else ''}"


def build_payload(snapshots: list[ProviderSnapshot], at: str) -> dict:
    """The JSON envelope.

    `directives` is a list with one entry per meter, each naming the provider
    and window it applies to. There is deliberately no single combined
    instruction: one multiplier for everything reads as advice about your whole
    workload when it only ever described one window.
    """
    return {
        "at": at,
        "directives": directives(snapshots),
        "providers": [s.to_dict() for s in snapshots],
    }


def _change_cell(pace) -> str:
    """`-93%` / `+47%` / `-`, as one whitespace-free awk-friendly token.

    Signed rather than worded so `awk '$10+0 < -50'` picks out the meters that
    need real throttling without matching on the verdict string.
    """
    if pace is None or pace.change_percent is None:
        return "-"
    sign = "-" if pace.direction == "down" else "+"
    # No "at the cap" marker here, unlike the TUI: this column is read by
    # scripts, and a trailing symbol on a number invites a parsing bug for
    # information the reader can get from the capped value itself.
    return f"{sign}{pace.change_percent:.0f}%"


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
            if gauge.active_limit:
                flags.append("ACTIVE")
            if snap.stale:
                flags.append("STALE")
            pace = gauge.pace()
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
                        # Appended, never inserted: existing scripts index
                        # columns 1-6 and must keep working.
                        (pace.verdict if pace else "-"),
                        # Absolute reset moment, whitespace-free so the column
                        # count stays fixed: 2026-08-08T03:32 local.
                        (gauge.resets_at.astimezone().strftime("%Y-%m-%dT%H:%M")
                         if gauge.resets_at else "-"),
                        format_remaining(gauge.seconds_remaining()).replace(" ", "")
                        or "-",
                        # Signed, so the sign alone is the instruction: -93%
                        # means ease off by 93%, +47% means there is room for
                        # 47% more. Same number the TUI draws beside its arrow.
                        _change_cell(pace),
                    ),
                )
            )
    if not rows:
        return "no usage data\n"

    columns = 10
    data = [cells for kind, cells in rows if kind == "data"]
    widths = (
        [max(len(cells[i]) for cells in data) for i in range(columns)]
        if data
        else [0] * columns
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
            pace = gauge.pace()
            if pace:
                arrow_tint = PACE_ANSI.get(pace.verdict, "") if color else ""
                arrow_off = RESET if color and arrow_tint else ""
                note = f"{arrow_tint}{pace.display}{arrow_off}"
            else:
                note = ""
            resets = format_reset_at(gauge.resets_at, "full")
            left = format_remaining(gauge.seconds_remaining())
            lines.append(
                f"  {gauge.label:<24}{tint}{bar}{off} "
                f"{tint}{gauge.percent:>3.0f}%{off}  "
                f"resets {resets}  ({left:>7})  {note}"
            )
    if not lines:
        return "no usage data\n"

    # One key under everything, exactly as the TUI draws it. No prose block
    # restating the rows: the instruction now lives on the row it applies to.
    lines.append("")
    lines.append(_legend(color))
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
            "reading you can pipe or paste.\n\n"
            f"{BANNER}. MIT licensed. Interface built with Textual."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=BANNER)
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
    parser.add_argument(
        "-w", "--watch", action="store_true",
        help="keep sampling on --interval instead of exiting; with --json this "
             "emits one JSON object per line for another service to subscribe to",
    )
    parser.add_argument(
        "--max-age", type=float, default=cache.DEFAULT_MAX_AGE, metavar="SECONDS",
        help=(
            f"reuse a cached reading this fresh instead of calling the API "
            f"(default: {cache.DEFAULT_MAX_AGE:.0f}). The cache is shared "
            f"between all afg processes, so a status bar polling every second "
            f"still costs one request a minute."
        ),
    )
    parser.add_argument(
        "--no-cache", action="store_true",
        help="always call the API (ignores --max-age; can earn you a 429)",
    )
    parser.add_argument(
        "--clear-cache", action="store_true",
        help="delete cached readings and any standing rate-limit backoff",
    )
    parser.add_argument(
        "--update", action="store_true",
        help="fetch the latest version and reinstall",
    )
    return parser


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _emit_once(snapshots: list[ProviderSnapshot], args) -> None:
    if args.json:
        # One object per line, flushed immediately: a consumer reading the pipe
        # gets each sample as it happens rather than when a buffer fills.
        json.dump(build_payload(snapshots, _now()), sys.stdout,
                  indent=None if args.watch else 2)
        sys.stdout.write("\n")
        sys.stdout.flush()
    elif args.pretty:
        color = not args.no_color and sys.stdout.isatty()
        sys.stdout.write(render_pretty(snapshots, color))
        sys.stdout.flush()
    else:
        sys.stdout.write(render_plain(snapshots))
        sys.stdout.flush()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.clear_cache:
        cache.clear()
        print(f"cleared {cache.cache_dir()}")
        return 0

    if args.update:
        from .update import update

        return update()

    max_age = 0.0 if args.no_cache else args.max_age
    if args.demo:
        from .demo import fetch_demo as _demo

        async def fetcher():
            return await _demo()
    else:

        async def fetcher():
            return await fetch_all(max_age)

    if args.watch:
        interval = max(MIN_INTERVAL, args.interval)
        try:
            while True:
                _emit_once(asyncio.run(fetcher()), args)
                time.sleep(interval)
        except KeyboardInterrupt:
            return 0

    if args.check or args.json or args.pretty:
        snapshots = asyncio.run(fetcher())
        _emit_once(snapshots, args)
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
