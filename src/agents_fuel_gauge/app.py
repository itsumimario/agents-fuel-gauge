"""The Textual UI: one panel per provider, one bar per quota window."""

from __future__ import annotations

import re
from datetime import datetime
from math import ceil

from rich.cells import cell_len
from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.widgets import Footer, Header, Static
from textual_plotext import PlotextPlot

from . import history
from .models import (
    PACE_ARROW,
    PACE_TOLERANCE,
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
    "window_over": "dim",
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
    "advice sits on the tightest safe meter — a too-new overlapping "
    "window can veto speed-up"
)


def color_of(gauge: Gauge) -> str:
    """Bar colour tracks how full the meter is, which is a different axis from
    the pace arrow's colour — a red bar with a green arrow is a real state:
    nearly spent, but the window resets before that matters."""
    return SEVERITY_COLOR.get(gauge.severity, "green")


def _axis_label(timestamp: float, visible_seconds: float) -> str:
    """Three short wall-clock anchors at the scale the user can actually see."""
    local = datetime.fromtimestamp(timestamp).astimezone()
    if visible_seconds >= 2 * 86_400:
        return local.strftime("%a %d")
    if visible_seconds >= 86_400:
        return local.strftime("%a %H:%M")
    return local.strftime("%H:%M")


def _segment_duration(seconds: float) -> str:
    """Keep every regime compact while retaining useful duration scale."""
    if seconds < 86_400:
        return f"{seconds / 3_600:.1f}h"
    return f"{seconds / 86_400:.1f}d"


def _percent_at(samples: list[history.Sample], timestamp: float) -> float:
    """Interpolate the integer-percent staircase at an inferred corner."""
    points = sorted(samples, key=lambda sample: sample["t"])
    if timestamp <= points[0]["t"]:
        return points[0]["pct"]
    if timestamp >= points[-1]["t"]:
        return points[-1]["pct"]
    for left, right in zip(points, points[1:]):
        if left["t"] <= timestamp <= right["t"]:
            span = right["t"] - left["t"]
            if span <= 0:
                return right["pct"]
            progress = (timestamp - left["t"]) / span
            return left["pct"] + (right["pct"] - left["pct"]) * progress
    return points[-1]["pct"]


def _segment_viewport(
    samples: list[history.Sample],
    segment: history.Segment | history.Episode,
) -> tuple[float, float, float, float]:
    """Frame one inferred regime tightly enough to be legible on a phone."""
    points = sorted(samples, key=lambda sample: sample["t"])
    duration = max(1.0, segment.end - segment.start)
    padding = duration * 0.08
    x_min = max(points[0]["t"], segment.start - padding)
    x_max = min(points[-1]["t"], segment.end + padding)
    if x_max <= x_min:
        x_max = x_min + 1.0

    visible = [
        sample["pct"]
        for sample in points
        if x_min <= sample["t"] <= x_max
    ]
    fitted_start = _percent_at(points, segment.start)
    fitted_delta = (
        segment.rate_per_day * (segment.end - segment.start) / 86_400
        if not isinstance(segment, history.Episode) or segment.linear
        else segment.delta_pct
    )
    fitted_end = fitted_start + fitted_delta
    values = [*visible, fitted_start, fitted_end]
    low = min(values)
    high = max(values)
    y_padding = max(0.5, (high - low) * 0.08)
    return x_min, x_max, max(0.0, low - y_padding), high + y_padding


def _segment_range(segment: history.Segment | history.Episode) -> str:
    """A compact local-time range that still disambiguates crossed dates."""
    start = datetime.fromtimestamp(segment.start).astimezone()
    end = datetime.fromtimestamp(segment.end).astimezone()
    if start.date() == end.date():
        return f"{start:%a %H:%M}–{end:%H:%M}"
    return f"{start:%a %H:%M}–{end:%a %H:%M}"


def _history_viewport(
    gauge: Gauge,
    samples: list[history.Sample],
    full_window: bool,
) -> tuple[float, float, float, float]:
    """Return x-min, x-max, y-min, y-max for a history chart.

    The complete quota window is useful context and a poor default on a phone:
    a young trace wastes the right side on days that have not happened, while a
    trace first observed near reset wastes the left side on days we never saw.
    Detailed mode frames the recorded interval with a little breathing room and
    fits the percent scale to the lines visible there. Full mode is retained as
    an explicit comparison rather than making detail irreversible.
    """
    reset = gauge.resets_at.timestamp()
    window = float(gauge.window_seconds)
    opened = reset - window
    percentages = [sample["pct"] for sample in samples]

    if full_window:
        return opened, reset, 0.0, max(100.0, max(percentages))

    first_sample = max(opened, min(sample["t"] for sample in samples))
    last_sample = min(reset, max(sample["t"] for sample in samples))
    recorded_seconds = max(1.0, last_sample - first_sample)
    padding = recorded_seconds * 0.05
    x_min = max(opened, first_sample - padding)
    x_max = min(reset, last_sample + padding)
    if x_max <= x_min:
        x_max = min(reset, x_min + 1.0)

    ideal_at_start = (x_min - opened) / window * 100.0
    ideal_at_end = (x_max - opened) / window * 100.0
    visible_values = [*percentages, ideal_at_start, ideal_at_end]
    if last_sample < reset and x_max > last_sample:
        required_at_end = percentages[-1] + (
            (100.0 - percentages[-1])
            * (x_max - last_sample)
            / (reset - last_sample)
        )
        visible_values.append(required_at_end)

    low = min(visible_values)
    high = max(visible_values)
    y_padding = max(0.5, (high - low) * 0.05)
    return x_min, x_max, max(0.0, low - y_padding), high + y_padding


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
        # Slack actionable advice stays quiet, but status is not advice. A
        # non-governing weekly meter still needs to say "too new" rather than
        # leaving a blank that looks like missing data.
        if pace is not None and pace.actionable and not self._governs():
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

        # Textual truncates a too-long border subtitle from the right. Keep the
        # masked account stable after layout instead of flashing it once at
        # mount and then replacing it on the first one-second age refresh. The
        # plan yields first; a compact age keeps freshness beside the account
        # at phone widths where the fully punctuated form does not fit.
        age = format_age(self.snapshot.captured_at)
        compact_age = "now" if age == "just now" else age.removesuffix(" ago")
        # Width is 0 until the first layout pass. Assume there is room rather
        # than degrade on an unknown, or the panel flashes a stripped subtitle
        # before the first tick corrects it.
        budget = (
            self.size.width - len(self.snapshot.display_name) - 6
            if self.size.width
            else 10_000
        )
        full_candidates = (
            " · ".join(
                p for p in [self.snapshot.plan, self.snapshot.account, age] if p
            ),
            " · ".join(p for p in [self.snapshot.account, age] if p),
            " ".join(p for p in [self.snapshot.account, compact_age] if p),
            self.snapshot.account or "",
            " · ".join(p for p in [self.snapshot.plan, age] if p),
            age,
        )
        for candidate in full_candidates:
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


class HistoryLegend(Static):
    """Decode the graph's line colours and styles without stealing plot room."""

    def render(self) -> Text:
        text = Text(no_wrap=False, overflow="fold")
        lines = [
            ("━━", "green", "normal usage"),
            ("━━", "yellow", "warning usage"),
            ("━━", "red", "critical usage"),
        ]
        if getattr(self.app, "history_details", False):
            lines.append(("━━", "dim grey70", "fitted linear portion"))
        else:
            lines.extend(
                [
                    ("━━", "dim grey70", "ideal budget pace"),
                    ("••", "green", "required path to 100% at reset"),
                ]
            )
        for mark, style, meaning in lines:
            text.append(mark, style=style)
            text.append(f" {meaning}   ", style="dim")
        return text


class ResponsiveFooter(Footer):
    """Keep every clickable key visible, using a second row when necessary."""

    def compose(self) -> ComposeResult:
        yield from super().compose()
        self.call_after_refresh(self._sync_layout)

    def bindings_changed(self, screen) -> None:
        super().bindings_changed(screen)
        self.call_after_refresh(self._sync_layout)

    def on_resize(self, event: events.Resize) -> None:
        self._sync_layout(event.size.width)

    def _shown_bindings(self):
        seen_actions: set[str] = set()
        for active in self.screen.active_bindings.values():
            binding = active.binding
            if not binding.show or binding.action in seen_actions:
                continue
            seen_actions.add(binding.action)
            yield binding

    def _sync_layout(self, width: int | None = None) -> None:
        bindings = list(self._shown_bindings())
        if not bindings:
            return
        available = self.size.width if width is None else width
        # FooterKey's default theme uses two cells around the key and one after
        # the description. Count terminal cells rather than code points so the
        # breakpoint remains correct if a key display is customized later.
        single_row_width = sum(
            cell_len(self.app.get_key_display(binding))
            + cell_len(binding.description)
            + 3
            for binding in bindings
        )
        wrapped = len(bindings) > 1 and single_row_width > available
        self.styles.grid_size_columns = (
            ceil(len(bindings) / 2) if wrapped else len(bindings)
        )
        self.set_class(wrapped, "-wrapped")


# Plotext calls ANSI colour 3 "orange" and silently drops names it does not
# know — including "yellow", the one Rich name the bars use for warning
# severity. A dropped colour leaves the trace in the widget theme's accent,
# so the severity most worth signalling is the one that would lose its
# colour. Translate explicitly rather than trusting Rich names to transfer.
PLOTEXT_SEVERITY = {
    "normal": "green",
    "warning": "orange",
    "critical": "red",
}


class UsageHistoryPlot(PlotextPlot):
    """A gauge trace in overview context or one detailed portion."""

    def __init__(
        self,
        gauge: Gauge,
        samples: list[history.Sample],
        full_window: bool = False,
        segment: history.Segment | history.Episode | None = None,
        segment_label: str = "",
    ) -> None:
        super().__init__()
        self.gauge = gauge
        self.samples = samples
        self.full_window = full_window
        self.segment = segment
        self.segment_label = segment_label

    def on_mount(self) -> None:
        super().on_mount()
        gauge = self.gauge
        reset = gauge.resets_at.timestamp()
        window = gauge.window_seconds
        opened = reset - window
        xs = [sample["t"] for sample in self.samples]
        ys = [sample["pct"] for sample in self.samples]
        if self.segment is None:
            x_min, x_max, y_min, y_max = _history_viewport(
                gauge, self.samples, self.full_window
            )
        else:
            x_min, x_max, y_min, y_max = _segment_viewport(
                self.samples, self.segment
            )
        midpoint = x_min + (x_max - x_min) / 2
        if self.segment is None:
            ideal_at_start = (x_min - opened) / window * 100
            ideal_at_end = (x_max - opened) / window * 100
            self.plt.plot(
                [x_min, x_max],
                [ideal_at_start, ideal_at_end],
                color="gray",
                style="dim",
            )
        elif not isinstance(self.segment, history.Episode) or self.segment.linear:
            fitted_start = _percent_at(self.samples, self.segment.start)
            fitted_delta = (
                self.segment.rate_per_day
                * (self.segment.end - self.segment.start)
                / 86_400
            )
            self.plt.plot(
                [self.segment.start, self.segment.end],
                [fitted_start, fitted_start + fitted_delta],
                color="gray",
                style="dim",
            )
        self.plt.plot(xs, ys, color=PLOTEXT_SEVERITY.get(gauge.severity, "green"))
        if self.segment is None and gauge.percent < 100:
            # Dot markers keep the chord distinguishable from the trace when a
            # normal-severity gauge makes both of them green.
            required_at_end = ys[-1] + (
                (100 - ys[-1]) * (x_max - xs[-1]) / (reset - xs[-1])
                if reset > xs[-1]
                else 0
            )
            self.plt.plot(
                [xs[-1], x_max],
                [ys[-1], required_at_end],
                color="green",
                marker="dot",
            )

        # Plotext's automatic ticks are dense enough to turn Unix timestamps
        # into a wall of digits. Three anchors across the visible range answer
        # the time question without competing with the trace.
        ticks = [x_min, midpoint, x_max]
        visible_seconds = x_max - x_min
        self.plt.xticks(
            ticks, [_axis_label(value, visible_seconds) for value in ticks]
        )
        y_midpoint = y_min + (y_max - y_min) / 2
        y_ticks = [y_min, y_midpoint, y_max]
        precision = 1 if y_max - y_min < 4 else 0
        self.plt.yticks(
            y_ticks, [f"{value:.{precision}f}%" for value in y_ticks]
        )
        self.plt.xlim(x_min, x_max)
        self.plt.ylim(y_min, y_max)
        self.plt.grid(horizontal=True, vertical=False)
        if self.segment is None:
            self.plt.title(f"Usage · {gauge.percent:.0f}%")
        else:
            self.plt.title(self.segment_label)


class HistorySegment(Vertical):
    """One shape-based portion, independently scaled with evidence beneath."""

    def __init__(
        self,
        gauge: Gauge,
        samples: list[history.Sample],
        segment: history.Episode,
        position: int,
    ) -> None:
        super().__init__(classes="history-segment")
        self.gauge = gauge
        self.samples = samples
        self.segment = segment
        self.position = position

    def compose(self) -> ComposeResult:
        label = "newest" if self.position == 0 else f"previous {self.position}"
        shape = "linear" if self.segment.linear else "variable"
        yield UsageHistoryPlot(
            self.gauge,
            self.samples,
            segment=self.segment,
            segment_label=f"{label} · {shape}",
        )
        rate_label = "fitted" if self.segment.linear else "average"
        yield Static(
            f"{_segment_range(self.segment)} · "
            f"{self.segment.delta_pct:+.1f}% over "
            f"{_segment_duration(self.segment.end - self.segment.start)} · "
            f"{rate_label} {self.segment.rate_per_day:.1f}%/d",
            classes="history-segment-summary",
        )


class ProviderHistory(Vertical):
    """One selected gauge for a provider, with rate-regime evidence."""

    def __init__(
        self,
        snapshot: ProviderSnapshot,
        full_window: bool = False,
        details: bool = False,
        gauge: Gauge | None = None,
    ) -> None:
        super().__init__(id=f"history-{snapshot.key}")
        self.snapshot = snapshot
        self.full_window = full_window
        self.details = details
        self.gauge = gauge or snapshot.tightest
        self.samples = (
            history.read_window(
                snapshot.key,
                self.gauge.label,
                self.gauge.resets_at,
                self.gauge.window_seconds,
            )
            if self.gauge is not None
            else []
        )
        self.inferred = history.segments(self.samples)
        self.portions = history.episodes(self.samples)
        if self.details:
            self.add_class("-details")

    def compose(self) -> ComposeResult:
        gauge = self.gauge
        if (
            gauge is None
            or gauge.resets_at is None
            or not gauge.window_seconds
            or len(self.samples) < 2
        ):
            yield Static(
                "no history yet — samples accrue as afg polls",
                classes="history-empty",
            )
            return

        if self.details:
            if not self.portions:
                yield Static(
                    "not enough history to classify segments yet",
                    classes="history-empty",
                )
                return
            for position, portion in enumerate(reversed(self.portions)):
                yield HistorySegment(gauge, self.samples, portion, position)
            return

        yield UsageHistoryPlot(gauge, self.samples, self.full_window)
        remaining = gauge.resets_at.timestamp() - self.samples[-1]["t"]
        if not self.inferred or remaining <= 0:
            yield Static(
                "not enough movement to infer a rate yet",
                classes="history-rate",
            )
            return

        required = max(0.0, 100 - self.samples[-1]["pct"]) / remaining * 86_400
        chunks = [
            f"{segment.rate_per_day:.1f}%/d "
            f"({_segment_duration(segment.end - segment.start)})"
            for segment in self.inferred
        ]
        latest_rate = self.inferred[-1].rate_per_day
        if latest_rate > required * (1 + PACE_TOLERANCE):
            chunks[-1] += f" {PACE_ARROW['slow_down']}"
        elif latest_rate < required * (1 - PACE_TOLERANCE):
            chunks[-1] += f" {PACE_ARROW['spare_capacity']}"
        yield Static(
            f"rate: {' → '.join(chunks)} · required {required:.1f}%/d",
            classes="history-rate",
        )

    def on_mount(self) -> None:
        self.border_title = self.snapshot.display_name
        if self.gauge is not None:
            self.border_title += f" — {self.gauge.label}"
        if self.details:
            count = len(self.portions)
            plural = "s" if count != 1 else ""
            self.border_subtitle = f"Segments · {count} portion{plural}"
        else:
            self.border_subtitle = (
                "Full range" if self.full_window else "Recorded range"
            )


class FuelGaugeApp(App):
    """Live view of Claude and Codex quota, scoped model caps included."""

    CSS_PATH = "app.tcss"
    TITLE = "agents fuel gauge"
    COMMAND_PALETTE_BINDING = "o"

    BINDINGS = [
        ("q", "quit", "Quit"),
        ("r", "refresh_now", "Refresh"),
        ("t", "set_theme('textual-light')", "Light"),
        ("t", "set_theme('textual-dark')", "Dark"),
        ("h", "set_dashboard_view('history')", "History"),
        ("h", "set_dashboard_view('gauges')", "Gauges"),
        ("z", "set_history_range('full')", "Full range"),
        ("z", "set_history_range('recorded')", "Recorded range"),
        ("m", "cycle_history_meter", "Next meter"),
        ("d", "set_history_mode('segments')", "Segments"),
        ("d", "set_history_mode('overview')", "Overview"),
        ("o", "command_palette", "Options"),
    ]

    def __init__(self, interval: float = 60.0, fetcher=None) -> None:
        super().__init__()
        self.interval = interval
        # Injectable so --demo and the screenshot script can supply synthetic
        # data without the network. Resolved here, not at import, so tests can
        # still monkeypatch the module-level fetch_all.
        self.fetcher = fetcher or fetch_all
        self.refresh_fetcher = fetcher or (lambda: fetch_all(0))
        self.snapshots: list[ProviderSnapshot] = []
        self.showing_history = False
        self.history_full_window = False
        self.history_details = False
        self.history_gauge_labels: dict[str, str] = {}

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield VerticalScroll(id="panels")
        plots = VerticalScroll(id="plots")
        plots.display = False
        yield plots
        history_legend = HistoryLegend(id="history-legend")
        history_legend.display = False
        yield history_legend
        # Below both panels, as one key for both, so it reads as a key rather
        # than as a message about whichever panel it happens to sit next to.
        legend = Legend(id="legend")
        legend.display = False
        yield legend
        yield ResponsiveFooter(show_command_palette=False)

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

    async def poll(self, *, force: bool = False) -> None:
        try:
            fetcher = self.refresh_fetcher if force else self.fetcher
            fetched = await fetcher()
        except Exception as exc:  # never let a poll kill the app
            self.sub_title = f"fetch failed: {exc.__class__.__name__}"
            return

        panels = self.query_one("#panels", VerticalScroll)
        plots = self.query_one("#plots", VerticalScroll)
        installed = [snapshot for snapshot in fetched if snapshot.installed]
        installed_keys = {snapshot.key for snapshot in installed}

        # A CLI can disappear while a long-running dashboard is open. Remove
        # that provider just as decisively as we omit it on first launch; stale
        # panels are useful for failed polls, not for uninstalled products.
        for panel in list(self.query(ProviderPanel)):
            if panel.snapshot.key not in installed_keys:
                await panel.remove()

        placeholders = list(panels.query("#no-providers"))
        if not installed:
            self.snapshots = []
            self.showing_history = False
            self.refresh_bindings()
            panels.display = True
            plots.display = False
            await plots.remove_children()
            self.query_one(HistoryLegend).display = False
            self.query_one(Legend).display = False
            if not placeholders:
                await panels.mount(
                    Static(
                        "no supported agent CLIs found — install Claude Code "
                        "or Codex to begin",
                        id="no-providers",
                        classes="provider-empty",
                    )
                )
            self.sub_title = ""
            self._hide_cursor()
            return

        for placeholder in placeholders:
            await placeholder.remove()

        # Keep the merged snapshots, not the raw fetch, so the status line and
        # the panels agree about carried-forward data.
        merged: list[ProviderSnapshot] = []
        for snapshot in installed:
            existing = self.query(f"#panel-{snapshot.key}")
            if existing:
                panel = existing.first(ProviderPanel)
                snapshot = snapshot.carry_forward(panel.snapshot)
                await panel.update_snapshot(snapshot)
            else:
                await panels.mount(ProviderPanel(snapshot))
            merged.append(snapshot)

        self.snapshots = merged
        self.query_one(Legend).display = not self.showing_history
        self.query_one(HistoryLegend).display = self.showing_history
        if self.showing_history:
            await self._refresh_plots()

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
        await self.poll(force=True)

    async def _refresh_plots(self) -> None:
        """Rebuild only for new data or an explicit switch to history.

        Plotext rendering is materially heavier than changing a countdown
        label. Keeping it out of `_tick` is what lets the one-second timer stay
        cheap even while the history pane is visible.
        """
        plots = self.query_one("#plots", VerticalScroll)
        await plots.remove_children()
        await plots.mount_all(
            [
                ProviderHistory(
                    snapshot,
                    self.history_full_window,
                    gauge=self._selected_history_gauge(snapshot),
                )
                if not self.history_details
                else ProviderHistory(
                    snapshot,
                    details=True,
                    gauge=self._selected_history_gauge(snapshot),
                )
                for snapshot in self.snapshots
            ]
        )

    def _history_choices(self, snapshot: ProviderSnapshot) -> list[Gauge]:
        """Meters with enough recorded samples to draw, in dashboard order."""
        recorded = [
            gauge
            for gauge in snapshot.gauges
            if len(
                history.read_window(
                    snapshot.key,
                    gauge.label,
                    gauge.resets_at,
                    gauge.window_seconds,
                )
            )
            >= 2
        ]
        return recorded

    def _selected_history_gauge(self, snapshot: ProviderSnapshot) -> Gauge | None:
        choices = self._history_choices(snapshot)
        if not choices:
            # Preserve the existing empty-history panel while keeping Meter
            # hidden until there is more than one drawable series to cycle.
            return snapshot.tightest
        selected = self.history_gauge_labels.get(snapshot.key)
        gauge = next((g for g in choices if g.label == selected), None)
        if gauge is None:
            preferred = snapshot.tightest
            gauge = next(
                (g for g in choices if preferred and g.label == preferred.label),
                choices[0],
            )
        self.history_gauge_labels[snapshot.key] = gauge.label
        return gauge

    async def action_set_dashboard_view(self, view: str) -> None:
        if not self.snapshots:
            return
        self.showing_history = view == "history"
        self.refresh_bindings()
        self.query_one("#panels", VerticalScroll).display = not self.showing_history
        self.query_one(Legend).display = not self.showing_history
        self.query_one(HistoryLegend).display = self.showing_history
        self.query_one("#plots", VerticalScroll).display = self.showing_history
        if self.showing_history:
            await self._refresh_plots()

    async def action_set_history_range(self, range_name: str) -> None:
        if not self.showing_history or self.history_details or not self.snapshots:
            return
        self.history_full_window = range_name == "full"
        self.refresh_bindings()
        await self._refresh_plots()

    async def action_cycle_history_meter(self) -> None:
        if not self.showing_history or not self.snapshots:
            return
        changed = False
        for snapshot in self.snapshots:
            choices = self._history_choices(snapshot)
            if len(choices) < 2:
                continue
            selected = self._selected_history_gauge(snapshot)
            index = next(
                (
                    index
                    for index, gauge in enumerate(choices)
                    if selected and gauge.label == selected.label
                ),
                0,
            )
            self.history_gauge_labels[snapshot.key] = choices[
                (index + 1) % len(choices)
            ].label
            changed = True
        if changed:
            await self._refresh_plots()

    async def action_set_history_mode(self, mode: str) -> None:
        if not self.showing_history or not self.snapshots:
            return
        self.history_details = mode == "segments"
        self.refresh_bindings()
        self.query_one(HistoryLegend).refresh()
        await self._refresh_plots()

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action == "set_theme" and parameters:
            return self.theme != parameters[0]
        if action == "set_dashboard_view" and parameters:
            requested_history = parameters[0] == "history"
            return requested_history != self.showing_history
        if action == "set_history_range" and parameters:
            requested_full = parameters[0] == "full"
            return (
                self.showing_history
                and not self.history_details
                and bool(self.snapshots)
                and requested_full != self.history_full_window
            )
        if action == "cycle_history_meter":
            return (
                self.showing_history
                and any(
                    len(self._history_choices(snapshot)) > 1
                    for snapshot in self.snapshots
                )
            )
        if action == "set_history_mode" and parameters:
            requested_details = parameters[0] == "segments"
            return (
                self.showing_history
                and bool(self.snapshots)
                and requested_details != self.history_details
            )
        return super().check_action(action, parameters)

    def action_set_theme(self, theme: str) -> None:
        self.theme = theme
        self.refresh_bindings()
