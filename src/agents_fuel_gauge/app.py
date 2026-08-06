"""The Textual UI: one panel per provider, one bar per quota window."""

from __future__ import annotations

import re

from rich.text import Text
from textual.app import App, ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.widgets import Footer, Header, Static

from .models import (
    Gauge,
    ProviderSnapshot,
    directives,
    format_age,
    format_countdown,
    format_remaining,
    format_reset_at,
)
from .sources import fetch_all

BAR_FULL = "█"
BAR_EMPTY = "░"

# Layout budget. The bar is the only element allowed to shrink to nothing,
# because it is decoration for a number shown right beside it.
LABEL_WIDTH = 24
MIN_LABEL = 8
MIN_BAR = 6

SEVERITY_COLOR = {
    "normal": "green",
    "warning": "yellow",
    "critical": "red",
}

# Drift against budget, which is a different axis from how full the bar is:
# a nearly-full bar late in its window is fine, an early one is not.
PACE_MARK = {
    "slow_down": "↑ ",
    "spare_capacity": "↓ ",
    "on_track": "· ",
    "exhausted": "✗ ",
    "too_early": "◦ ",
}
PACE_COLOR = {
    "slow_down": "bold red",
    "spare_capacity": "green",
    "on_track": "dim",
    "exhausted": "bold red",
    "too_early": "dim",
}


class GaugeBar(Static):
    """A single quota bar, redrawn on resize and on every countdown tick."""

    def __init__(self, gauge: Gauge) -> None:
        super().__init__()
        self.gauge = gauge

    def update_gauge(self, gauge: Gauge) -> None:
        self.gauge = gauge
        self.refresh()

    def render(self) -> Text:
        """Lay the row out by priority, not by fixed columns.

        The reset time is the whole reason to look at a gauge, and it used to
        be the first thing lost: the bar had a minimum width, so on a narrow
        terminal the row overflowed and the right-hand edge — the countdown —
        got cropped. The bar is the least information-dense element here, so it
        is now the one that yields. It shrinks, then disappears entirely, before
        anything you actually read is touched.
        """
        gauge = self.gauge
        color = SEVERITY_COLOR.get(gauge.severity, "green")
        pace = gauge.pace()
        pace_mark = PACE_MARK.get(pace.verdict, "  ") if pace else "  "
        pace_style = PACE_COLOR.get(pace.verdict, "dim") if pace else "dim"

        remaining = gauge.seconds_remaining()
        relative = format_remaining(remaining)
        reset_full = format_reset_at(gauge.resets_at, "full")
        reset_short = format_reset_at(gauge.resets_at, "short")
        reset_time = format_reset_at(gauge.resets_at, "time")

        countdown = format_countdown(remaining)
        compact = countdown.replace(" ", "")
        # Coarsest readable form for the very narrowest terminals: the leading
        # unit only, "18h 07m" -> "18h". Less precise, but "roughly 18 hours"
        # beats no reset time at all, which is what cropping leaves you with.
        leading = re.match(r"\d+[dhms]", compact)
        coarse = leading.group(0) if leading else compact
        marker = "◆" if gauge.active_limit else " "
        width = self.size.width
        pct_style = f"bold {color}"

        # Tail variants, richest first, each as the exact segments that will be
        # drawn. Measuring the same list we render from is what keeps the two
        # in step — computing a width separately from the output invites them
        # to drift apart by a space, which is precisely how the countdown got
        # cropped before.
        # Both forms of "when does it reset" are shown wherever they fit: the
        # wall-clock moment to plan around, and the distance to it. The
        # absolute time is the first to go when space runs short, because the
        # relative one still answers "have I got time for this".
        variants = (
            ([(f"{gauge.percent:>4.0f}%", pct_style),
              (f"  {reset_full}", "dim"),
              (f"  {relative:>6}", "dim"),
              (f" {pace_mark}", pace_style)], 2, MIN_LABEL),
            ([(f"{gauge.percent:>4.0f}%", pct_style),
              (f"  {reset_short}", "dim"),
              (f"  {relative:>6}", "dim"),
              (f" {pace_mark}", pace_style)], 2, MIN_LABEL),
            ([(f"{gauge.percent:>4.0f}%", pct_style),
              (f"  {reset_time}", "dim"),
              (f" {relative:>6}", "dim"),
              (f" {pace_mark}", pace_style)], 2, MIN_LABEL),
            ([(f"{gauge.percent:>4.0f}%", pct_style),
              (f"  {relative}", "dim"),
              (f" {pace_mark}", pace_style)], 2, MIN_LABEL),
            ([(f"{gauge.percent:>4.0f}%", pct_style),
              (f" {compact}", "dim")], 2, MIN_LABEL),
            ([(f"{gauge.percent:.0f}%", pct_style),
              (f" {compact}", "dim")], 0, 3),
            ([(compact, "dim")], 0, 0),
            ([(coarse, "dim")], 0, 0),
        )

        tail, marker_width = variants[-1][0], 0
        for segments, mark_w, min_label in variants:
            if mark_w + min_label + sum(len(t) for t, _ in segments) <= width:
                tail, marker_width = segments, mark_w
                break

        tail_width = sum(len(t) for t, _ in tail)
        available = max(0, width - marker_width - tail_width)

        if available >= LABEL_WIDTH + MIN_BAR + 1:
            label_width = LABEL_WIDTH
            bar_width = available - label_width - 1
        elif available >= MIN_LABEL + MIN_BAR + 1:
            bar_width = MIN_BAR
            label_width = available - bar_width - 1
        else:
            # Too tight for any bar. Let the label shrink past its usual floor
            # rather than push the countdown off the edge.
            bar_width = 0
            label_width = available

        label = gauge.label
        if label_width and len(label) > label_width:
            # "text…" is one char longer than the text it replaces, so with a
            # single column to spend the ellipsis has to stand alone.
            label = (label[: label_width - 1] + "…") if label_width >= 2 else "…"

        text = Text(no_wrap=True, overflow="crop")
        if marker_width:
            text.append(
                f"{marker} ", style=f"bold {color}" if gauge.active_limit else "dim"
            )
        if label_width:
            text.append(
                f"{label:<{label_width}}",
                style="bold" if gauge.active_limit else "",
            )
        if bar_width:
            filled = min(bar_width, round(gauge.percent / 100 * bar_width))
            text.append(" ")
            text.append(BAR_FULL * filled, style=color)
            text.append(BAR_EMPTY * (bar_width - filled), style="bright_black")
        for segment, style in tail:
            text.append(segment, style=style)
        return text


class ProviderPanel(Vertical):
    """A bordered box of gauges for one agent CLI."""

    def __init__(self, snapshot: ProviderSnapshot) -> None:
        super().__init__(id=f"panel-{snapshot.key}")
        self.snapshot = snapshot

    def compose(self) -> ComposeResult:
        yield from self._rows()

    def on_mount(self) -> None:
        self.refresh_titles()

    def _rows(self):
        snapshot = self.snapshot
        if snapshot.error:
            # Stale-but-present data is a warning; no data at all is an error.
            style = "yellow" if snapshot.stale else "red"
            prefix = "⚠  showing last known — " if snapshot.stale else "⚠  "
            yield Static(
                Text(f"{prefix}{snapshot.error}", style=style), classes="issue"
            )
        if snapshot.gauges:
            for gauge in snapshot.gauges:
                yield GaugeBar(gauge)
        elif not snapshot.error:
            yield Static(Text("no quota windows reported", style="dim"), classes="issue")

    def refresh_titles(self) -> None:
        """Each panel carries its own freshness.

        Providers are polled concurrently and fail independently, so one global
        'updated at' would be a lie the moment either side errors or retries.
        """
        self.border_title = self.snapshot.display_name

        # Textual truncates a too-long border subtitle from the right, which is
        # where the age sits — the one part that changes. So drop the account,
        # then the plan, rather than let the freshness be silently cut off.
        age = format_age(self.snapshot.captured_at)
        # Width is 0 until the first layout pass. Assume there is room rather
        # than degrade on an unknown, or the panel flashes a stripped subtitle
        # before the first tick corrects it.
        budget = (
            self.size.width - len(self.snapshot.display_name) - 6
            if self.size.width
            else 10_000
        )
        for parts in (
            [self.snapshot.plan, self.snapshot.account, age],
            [self.snapshot.plan, age],
            [age],
        ):
            candidate = " · ".join(p for p in parts if p)
            if len(candidate) <= budget:
                break
        self.border_subtitle = candidate

    async def update_snapshot(self, snapshot: ProviderSnapshot) -> None:
        """Update in place when the shape is unchanged; rebuild when it isn't.

        Remounting every poll would flicker, but a scoped limit can appear or
        vanish upstream, so compare the label set and only rebuild on a change.
        """
        previous = self.snapshot
        self.snapshot = snapshot
        self.refresh_titles()

        old_labels = [g.label for g in previous.gauges]
        new_labels = [g.label for g in snapshot.gauges]
        same_shape = (
            old_labels == new_labels
            and previous.error == snapshot.error
            and previous.stale == snapshot.stale
        )

        if same_shape and new_labels:
            for widget, gauge in zip(self.query(GaugeBar), snapshot.gauges):
                widget.update_gauge(gauge)
            return

        await self.remove_children()
        await self.mount_all(list(self._rows()))


class StatusLine(Static):
    """Per-meter advice, one line each, every line naming its own meter.

    This used to rank all gauges together and announce a single winner as
    "runs out first", with one rate multiplier. Both halves were wrong. The
    ranking implied a prediction the data cannot make — `used / elapsed` is an
    average over the whole window, so a meter burned hard days ago and idle
    since is indistinguishable from one burning steadily now, and it would be
    named the winner on the strength of usage that had already stopped. And a
    bare multiplier read as guidance for everything you were running, when it
    only ever described one window.

    So: no cross-provider ranking, and no unattributed instruction.
    """

    def set_snapshots(self, snapshots: list[ProviderSnapshot]) -> None:
        rows = [d for d in directives(snapshots) if d["actionable"]]
        # Only the meters actually drifting are worth a line; listing the
        # healthy ones buries the two that matter.
        notable = [d for d in rows if d["verdict"] in ("slow_down", "exhausted")]

        text = Text(no_wrap=False, overflow="fold")
        if not rows:
            text.append("no pace estimate yet", style="dim")
        elif not notable:
            text.append("every meter is within its budget", style="green")
        else:
            for index, row in enumerate(notable):
                if index:
                    text.append("\n")
                name = row["provider"].capitalize()
                text.append(f"{name} {row['label']}", style="bold")
                text.append(": ", style="dim")
                text.append(
                    row["advice"], style=PACE_COLOR.get(row["verdict"], "dim")
                )
        self.update(text)


class FuelGaugeApp(App):
    """Live view of Claude and Codex quota, scoped model caps included."""

    CSS_PATH = "app.tcss"
    TITLE = "agents fuel gauge"

    BINDINGS = [
        ("q", "quit", "Quit"),
        ("r", "refresh_now", "Refresh"),
        ("t", "cycle_theme", "Theme"),
    ]

    def __init__(self, interval: float = 60.0, fetcher=None) -> None:
        super().__init__()
        self.interval = interval
        # Injectable so --demo and the screenshot script can supply synthetic
        # data without the network. Resolved here, not at import, so tests can
        # still monkeypatch the module-level fetch_all.
        self.fetcher = fetcher or fetch_all
        self.snapshots: list[ProviderSnapshot] = []

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield StatusLine(Text("loading…", style="dim"), id="status")
        yield VerticalScroll(id="panels")
        yield Footer()

    def on_mount(self) -> None:
        self.sub_title = "fetching…"
        # Textual's default header icon is U+2B58, which plenty of monospace
        # fonts lack — it shows up as a tofu box. U+2261 is far better covered
        # and still reads as "menu".
        self.query_one(Header).icon = "≡"
        self._hide_cursor()
        # Countdowns tick locally every second; the network is only hit on
        # `interval`, so a 1s display refresh costs nothing.
        self.set_interval(1.0, self._tick)
        self.set_interval(self.interval, self.poll)
        self.call_later(self.poll)

    def _hide_cursor(self) -> None:
        """Re-assert DECTCEM.

        Textual hides the cursor when it takes over the screen, but a terminal
        multiplexer or a mosh session in between can put it back — leaving a
        cursor blinking over the UI. Re-asserting after each redraw is cheap
        and idempotent. Never let this break the app.
        """
        driver = getattr(self, "_driver", None)
        if driver is None:
            return
        try:
            driver.write("\x1b[?25l")
            getattr(driver, "flush", lambda: None)()
        except Exception:
            pass

    def _tick(self) -> None:
        for bar in self.query(GaugeBar):
            bar.refresh()
        for panel in self.query(ProviderPanel):
            panel.refresh_titles()  # keeps each panel's "12s ago" honest
        self.query_one(StatusLine).set_snapshots(self.snapshots)

    async def poll(self) -> None:
        try:
            fetched = await self.fetcher()
        except Exception as exc:  # never let a poll kill the app
            self.sub_title = f"fetch failed: {exc.__class__.__name__}"
            return

        panels = self.query_one("#panels", VerticalScroll)
        # Keep the merged snapshots, not the raw fetch, so the status line and
        # the panels agree about carried-forward data.
        merged: list[ProviderSnapshot] = []
        for snapshot in fetched:
            existing = self.query(f"#panel-{snapshot.key}")
            if existing:
                panel = existing.first(ProviderPanel)
                snapshot = snapshot.carry_forward(panel.snapshot)
                await panel.update_snapshot(snapshot)
            else:
                await panels.mount(ProviderPanel(snapshot))
            merged.append(snapshot)

        self.snapshots = merged
        self.query_one(StatusLine).set_snapshots(self.snapshots)

        # No global "updated at" here — each panel reports its own age, because
        # providers are fetched concurrently and one can go stale while the
        # other keeps refreshing.
        problems = [s.display_name for s in merged if not s.ok]
        self.sub_title = (
            f"{', '.join(problems)} not updating" if problems else ""
        )
        self._hide_cursor()

    async def action_refresh_now(self) -> None:
        self.sub_title = "refreshing…"
        await self.poll()

    def action_cycle_theme(self) -> None:
        self.theme = (
            "textual-light" if self.theme == "textual-dark" else "textual-dark"
        )
