"""The Textual UI: one panel per provider, one bar per quota window."""

from __future__ import annotations

from rich.text import Text
from textual.app import App, ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.widgets import Footer, Header, Static

from .models import Gauge, ProviderSnapshot, format_age, format_countdown
from .sources import fetch_all

BAR_FULL = "█"
BAR_EMPTY = "░"

SEVERITY_COLOR = {
    "normal": "green",
    "warning": "yellow",
    "critical": "red",
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
        gauge = self.gauge
        color = SEVERITY_COLOR.get(gauge.severity, "green")

        label_width, meter_width, reset_width = 26, 6, 10
        bar_width = max(8, self.size.width - label_width - meter_width - reset_width - 2)
        filled = min(bar_width, round(gauge.percent / 100 * bar_width))

        marker = "◆" if gauge.runs_out_first else " "
        label = gauge.label
        if len(label) > label_width - 3:
            label = label[: label_width - 4] + "…"

        text = Text(no_wrap=True, overflow="crop")
        text.append(f"{marker} ", style=f"bold {color}" if gauge.runs_out_first else "dim")
        text.append(f"{label:<{label_width - 2}}", style="bold" if gauge.runs_out_first else "")
        text.append(BAR_FULL * filled, style=color)
        text.append(BAR_EMPTY * (bar_width - filled), style="bright_black")
        text.append(f"{gauge.percent:>5.0f}%", style=f"bold {color}")
        text.append(
            f"  {format_countdown(gauge.seconds_remaining()):>{reset_width}}",
            style="dim",
        )
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
        parts = [self.snapshot.subtitle, format_age(self.snapshot.captured_at)]
        self.border_subtitle = " · ".join(p for p in parts if p)

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
    """One line naming the quota that will stop you before any of the others."""

    def set_snapshots(self, snapshots: list[ProviderSnapshot]) -> None:
        candidates = [
            (snap, gauge)
            for snap in snapshots
            for gauge in snap.gauges
            if gauge.runs_out_first
        ] or [
            (snap, snap.worst) for snap in snapshots if snap.worst is not None
        ]
        if not candidates:
            self.update(Text("no usage data", style="dim"))
            return

        snap, gauge = max(candidates, key=lambda pair: pair[1].percent)
        color = SEVERITY_COLOR.get(gauge.severity, "green")
        text = Text(no_wrap=True, overflow="ellipsis")
        text.append("runs out first: ", style="dim")
        text.append(f"{snap.display_name} {gauge.label}", style=f"bold {color}")
        text.append("  ", style="")
        text.append(f"{gauge.percent:.0f}% used", style=f"bold {color}")
        remaining = format_countdown(gauge.seconds_remaining())
        if remaining:
            text.append(f"  ·  resets in {remaining}", style="dim")
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

    def __init__(self, interval: float = 60.0) -> None:
        super().__init__()
        self.interval = interval
        self.snapshots: list[ProviderSnapshot] = []

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield StatusLine(Text("loading…", style="dim"), id="status")
        yield VerticalScroll(id="panels")
        yield Footer()

    def on_mount(self) -> None:
        self.sub_title = "fetching…"
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
            fetched = await fetch_all()
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
