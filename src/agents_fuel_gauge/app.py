"""The Textual UI: one panel per provider, one bar per quota window."""

from __future__ import annotations

import re

from rich.text import Text
from textual.app import App, ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.widgets import Footer, Header, Static

from .models import (
    PACE_ARROW,
    Gauge,
    ProviderSnapshot,
    format_age,
    format_countdown,
    format_remaining,
    format_reset_at,
    governing_indexes,
)
from .sources import fetch_all

BAR_FULL = "█"
BAR_EMPTY = "░"

# Layout budget. The bar is the only element allowed to shrink to nothing,
# because it is decoration for a number shown right beside it.
LABEL_WIDTH = 24
MIN_LABEL = 8
MIN_BAR = 6
SEP = "  "

# What a row can show, richest first, as the columns it draws. Both forms of
# "when does it reset" appear wherever they fit: the wall-clock moment to plan
# around, and the distance to it. The absolute time is the first to go when
# space runs short, because the relative one still answers "have I got time for
# this" — and the pace column outlives both, because what to do about a meter
# outranks exactly when it resets.
ROW_VARIANTS = (
    (("pct", "reset_full", "relative", "pace_full"), MIN_LABEL),
    (("pct", "reset_short", "relative", "pace_full"), MIN_LABEL),
    (("pct", "reset_time", "relative", "pace_full"), MIN_LABEL),
    (("pct", "relative", "pace_full"), MIN_LABEL),
    # Below here the magnitude no longer fits. The bare arrow still says which
    # way to move, which is most of the value.
    (("pct", "relative", "pace_mark"), MIN_LABEL),
    (("pct", "compact"), MIN_LABEL),
    (("pct", "compact"), 3),
    (("compact",), 0),
    (("coarse",), 0),
)

SEVERITY_COLOR = {
    "normal": "green",
    "warning": "yellow",
    "critical": "red",
}

# Colour reinforces the arrow rather than repeating the bar: red is "ease off",
# green is "there is room". Note this is a different axis from bar colour, which
# is about how full the meter is — a red bar with a green arrow is a real and
# useful state (nearly spent, but the window resets before it matters).
PACE_COLOR = {
    "slow_down": "bold red",
    "exhausted": "bold red",
    "spare_capacity": "green",
    "on_track": "dim",
    "too_early": "dim",
}

# Every glyph that can appear in a row, spelled out once beneath the panels.
# The rule is simple: any symbol on screen has to be decodable from the screen.
# It is short now because most states no longer use a symbol at all — "on pace"
# and "too new" print as those words, and a word needs no key.
LEGEND = (
    ("slow_down", "slow down"),
    ("spare_capacity", "speed up"),
    ("exhausted", "spent"),
)
LEGEND_NOTE = (
    "the % is how far to change that meter's average rate so far, "
    "so it lasts until it resets"
)
# Why most rows have no advice on them. Without this the blank column reads as
# missing data rather than as "this meter is not what is stopping you".
LEGEND_GOVERNS = (
    "advice sits on the tightest meter only — one request spends from "
    "all of them, so the others' slack is not yours to spend"
)


def color_of(gauge: Gauge) -> str:
    """Bar colour tracks how full the meter is, which is a different axis from
    the pace arrow's colour — a red bar with a green arrow is a real state:
    nearly spent, but the window resets before that matters."""
    return SEVERITY_COLOR.get(gauge.severity, "green")


class GaugeBar(Static):
    """A single quota bar, redrawn on resize and on every countdown tick."""

    def __init__(self, gauge: Gauge) -> None:
        super().__init__()
        self.gauge = gauge

    def update_gauge(self, gauge: Gauge) -> None:
        self.gauge = gauge
        self.refresh()

    def cells(self) -> dict[str, tuple[str, str, str]]:
        """Every value this row could print, as `key -> (text, style, align)`.

        Unpadded on purpose. Widths are a property of the *panel*, not of one
        row — see `render` — so each row publishes its raw values and lets the
        panel decide how wide each column has to be.
        """
        gauge = self.gauge
        pace = gauge.pace()
        pace_style = PACE_COLOR.get(pace.verdict, "dim") if pace else "dim"

        # The arrow is an instruction, and an instruction without a size is only
        # half of one — "slow down" begs "by how much?". The states with no
        # direction print as words instead of a glyph, so nothing in this column
        # has to be looked up (see `Pace.display`).
        #
        # A meter only speaks if it is what constrains you. Its neighbours share
        # the same budget, so their headroom is not spendable and printing it
        # invited exactly the wrong move: "↑ by 150%" on a 5h row while the 7d
        # row said "↓ by 93%". See `governing_indexes`.
        if not self._governs():
            pace = None
        arrow = pace.arrow if pace else ""
        remaining = gauge.seconds_remaining()
        compact = format_countdown(remaining).replace(" ", "")
        # Coarsest readable form for the very narrowest terminals: the leading
        # unit only, "18h07m" -> "18h". Less precise, but "roughly 18 hours"
        # beats no reset time at all, which is what cropping leaves you with.
        leading = re.match(r"\d+[dhms]", compact)

        return {
            "pct": (f"{gauge.percent:.0f}%", f"bold {color_of(gauge)}", ">"),
            "reset_full": (format_reset_at(gauge.resets_at, "full"), "dim", "<"),
            "reset_short": (format_reset_at(gauge.resets_at, "short"), "dim", "<"),
            "reset_time": (format_reset_at(gauge.resets_at, "time"), "dim", "<"),
            "relative": (format_remaining(remaining), "dim", ">"),
            "pace_full": (pace.display if pace else "", pace_style, "<"),
            # Terse fallback for the narrowest terminals. Only the directional
            # states survive it: if there is nothing to do, an empty column says
            # so more clearly than a glyph nobody can place.
            "pace_mark": (arrow, pace_style, "<"),
            "compact": (compact, "dim", ">"),
            "coarse": (leading.group(0) if leading else compact, "dim", ">"),
        }

    def _governs(self) -> bool:
        """Whether this meter is the one constraining the work it covers.

        Computed from the panel rather than stored, so it stays correct as the
        countdown ticks — which meter is tightest changes over time, not only
        when new data arrives. A bar rendered outside a panel (tests, one-off
        measurement) has nothing to be overruled by, so it speaks.
        """
        panel = self.parent
        rows = getattr(panel, "snapshot", None)
        if rows is None:
            return True
        siblings = [w for w in panel.children if isinstance(w, GaugeBar)]
        try:
            index = siblings.index(self)
        except ValueError:
            return True
        return index in governing_indexes(panel.snapshot)

    def _siblings(self) -> list["GaugeBar"]:
        """Every row that has to line up with this one.

        Screen-wide rather than panel-wide. The two boxes are the same width
        and sit one above the other, so they read as a single instrument: a bar
        that starts three columns further left in the Codex box than in the
        Claude box looks like a rendering fault, not like two tables. Rows of a
        different width are excluded so nothing is measured against a column
        budget it does not share.
        """
        try:
            rows = [
                w for w in self.screen.query(GaugeBar)
                if w.size.width == self.size.width
            ]
        except Exception:  # not mounted yet; measure against ourselves alone
            rows = []
        return rows or [self]

    def render(self) -> Text:
        """Lay the row out by priority, with columns sized across the panel.

        Two rules, and they interact:

        First, priority over fixed columns. The reset time is the whole reason
        to look at a gauge, and it used to be the first thing lost: the bar had
        a minimum width, so on a narrow terminal the row overflowed and the
        right-hand edge got cropped. The bar is the least information-dense
        element here, so it is the one that yields — it shrinks, then vanishes,
        before anything you actually read is touched.

        Second, the panel decides, not the row. Each row used to pick its own
        variant, which was fine while every row's tail was the same width. Once
        the pace column carried a magnitude that was no longer true: a row
        reading "· " had eight columns spare that a row reading "↓ by 92%" did
        not, so it chose a richer variant, and the bars and percentages in a
        single box no longer lined up. A jagged table reads as a broken one. So
        the variant is chosen once for the whole panel — the first that fits
        *every* row — and each column is padded to the widest value in it.
        """
        width = self.size.width
        rows = [w.cells() for w in self._siblings()]
        mine = self.cells()

        def widths(keys):
            return [max(len(r[k][0]) for r in rows) for k in keys]

        keys, cols = ROW_VARIANTS[-1][0], None
        for candidate, min_label in ROW_VARIANTS:
            measured = widths(candidate)
            # The gap before the tail only exists if there is a label to gap
            # from. The last-resort variants draw the countdown alone, and
            # charging them for a separator they never print is what pushed a
            # 5-char row into a 4-column widget.
            gap = len(SEP) if min_label else 0
            spans = sum(measured) + len(SEP) * (len(candidate) - 1)
            if min_label + gap + spans <= width:
                keys, cols = candidate, measured
                break
        if cols is None:
            cols = widths(keys)

        tail_width = sum(cols) + len(SEP) * (len(keys) - 1)
        available = max(0, width - tail_width)
        # Whatever is left has to cover the label, the bar, and the gap.
        block = max(0, available - len(SEP)) if available > len(SEP) else 0

        if block >= LABEL_WIDTH + MIN_BAR + 1:
            label_width = LABEL_WIDTH
            bar_width = block - label_width - 1
        elif block >= MIN_LABEL + MIN_BAR + 1:
            bar_width = MIN_BAR
            label_width = block - bar_width - 1
        else:
            # Too tight for any bar. Let the label shrink past its usual floor
            # rather than push the countdown off the edge.
            bar_width = 0
            label_width = block

        label = self.gauge.label
        if label_width and len(label) > label_width:
            # "text…" is one char longer than the text it replaces, so with a
            # single column to spend the ellipsis has to stand alone.
            label = (label[: label_width - 1] + "…") if label_width >= 2 else "…"

        text = Text(no_wrap=True, overflow="crop")
        if label_width:
            # Every row is styled identically. `active_limit` used to earn a ◆
            # and a bold label, but a symbol whose meaning is nowhere on screen
            # is a question, not information. The flag still ships in `--check`
            # (as the word ACTIVE) and in `--json`, where it can be spelled out.
            text.append(f"{label:<{label_width}}")
        if bar_width:
            filled = min(bar_width, round(self.gauge.percent / 100 * bar_width))
            text.append(" ")
            text.append(BAR_FULL * filled, style=color_of(self.gauge))
            text.append(BAR_EMPTY * (bar_width - filled), style="bright_black")
        drawn = bool(label_width or bar_width)
        spent = label_width + (1 + bar_width if bar_width else 0)
        for index, (key, column) in enumerate(zip(keys, cols)):
            value, style, align = mine[key]
            last = index == len(keys) - 1
            # Padding the final left-aligned column would only add invisible
            # trailing spaces, and the measurement above already reserved them.
            padded = value if (last and align == "<") else (
                value.rjust(column) if align == ">" else value.ljust(column)
            )
            segment = (SEP if index or drawn else "") + padded
            # Hard stop at the widget edge, so "the row fits" is a property of
            # the assembly rather than of the arithmetic above being right
            # everywhere. It matters at the extreme: a terminal narrow enough
            # to leave two columns after the panel border and a scrollbar has
            # no variant small enough, and something has to give way cleanly.
            room = width - spent
            if room <= 0:
                break
            segment = segment[:room]
            text.append(segment, style=style)
            spent += len(segment)
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


class Legend(Static):
    """One key for both panels, under both panels.

    This replaces a prose block that sat above the panels and restated, in
    sentences, what the rows were already showing — and restated it badly,
    because the sentence lived at the top while the meter it described lived
    somewhere below it, so you had to hold a name in your head and go looking.
    Put the instruction on the row and the key underneath, and neither problem
    exists: nothing has to be matched up, and the key is a fixed cost that does
    not grow with the number of meters.
    """

    def render(self) -> Text:
        # Wrapping, not truncating — a legend cut in half is a legend that
        # documents some of the symbols, which is arguably worse than none.
        text = Text(no_wrap=False, overflow="fold")
        for verdict, meaning in LEGEND:
            text.append(PACE_ARROW[verdict], style=PACE_COLOR[verdict])
            text.append(f" {meaning}   ", style="dim")
        # Its own line, so the break lands where it was chosen rather than
        # wherever the key happens to run out of terminal.
        text.append("\n")
        text.append(LEGEND_NOTE, style="dim")
        text.append("\n")
        text.append(LEGEND_GOVERNS, style="dim")
        return text


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
        yield VerticalScroll(id="panels")
        # Below both panels, as one key for both, so it reads as a key rather
        # than as a message about whichever panel it happens to sit next to.
        yield Legend(id="legend")
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
