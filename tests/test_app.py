"""Headless tests that drive the real Textual app with the network stubbed out."""

import re
from datetime import datetime, timedelta, timezone

import pytest

from agents_fuel_gauge import app as app_module
from agents_fuel_gauge.app import FuelGaugeApp, GaugeBar, Legend, ProviderPanel
from agents_fuel_gauge.models import (
    PACE_ARROW,
    Gauge,
    ProviderSnapshot,
    governing_indexes,
)


def governing_bars(app) -> list[GaugeBar]:
    """The rows that carry advice — the tightest meter in each pool.

    Meters share a budget, so only the tightest has anything actionable to say.
    Assertions about advice must be scoped to those rows; the rest are gauges.
    """
    out = []
    for panel in app.query(ProviderPanel):
        bars = [w for w in panel.children if isinstance(w, GaugeBar)]
        governors = governing_indexes(panel.snapshot)
        out += [b for i, b in enumerate(bars) if i in governors]
    return out

NOW = datetime.now(timezone.utc)
FIVE_HOURS = 5 * 3_600
ONE_WEEK = 7 * 86_400


def _snapshots(fable_percent: float = 91.0, with_scoped: bool = True):
    claude = ProviderSnapshot(
        key="claude",
        display_name="Claude",
        plan="Max 20x",
        account="a•••@e•••.com",
        captured_at=NOW,
        gauges=[
            Gauge("5h", "all models", 5.0, NOW + timedelta(hours=1),
                  window_seconds=FIVE_HOURS),
            Gauge("7d", "all models", 50.0, NOW + timedelta(hours=18),
                  window_seconds=ONE_WEEK),
        ],
    )
    if with_scoped:
        claude.gauges.append(
            Gauge(
                "7d",
                "Fable",
                fable_percent,
                NOW + timedelta(hours=18),
                severity="critical",
                active_limit=True,
                window_seconds=ONE_WEEK,
            )
        )
    codex = ProviderSnapshot(
        key="codex",
        display_name="Codex",
        plan="Pro",
        captured_at=NOW,
        gauges=[
            Gauge("7d", "all models", 98.0, NOW + timedelta(days=2), "critical", True,
                  window_seconds=ONE_WEEK)
        ],
    )
    return [claude, codex]


@pytest.fixture
def stub(monkeypatch):
    """Swap the network call for a controllable fake."""
    state = {"snapshots": _snapshots()}

    async def fake_fetch_all():
        return state["snapshots"]

    monkeypatch.setattr(app_module, "fetch_all", fake_fetch_all)
    return state


async def test_panels_and_bars_mount(stub):
    app = FuelGaugeApp(interval=3600)
    async with app.run_test() as pilot:
        await pilot.pause()
        panels = app.query(ProviderPanel)
        assert [p.snapshot.key for p in panels] == ["claude", "codex"]
        assert len(app.query(GaugeBar)) == 4


async def test_scoped_fable_bar_is_rendered(stub):
    app = FuelGaugeApp(interval=3600)
    async with app.run_test() as pilot:
        await pilot.pause()
        widget = next(b for b in app.query(GaugeBar) if b.gauge.scope == "Fable")
        assert widget.gauge.percent == 91.0
        assert widget.gauge.active_limit is True

        drawn = widget.render().plain
        assert "7d Fable" in drawn
        assert "91%" in drawn


async def test_no_undocumented_symbols_on_a_row(stub):
    """The ◆ that used to mark `active_limit` was unreadable on sight.

    Every glyph a row can draw now appears in the legend below the panels;
    anything else is a question mark rather than information.
    """
    app = FuelGaugeApp(interval=3600)
    async with app.run_test() as pilot:
        await pilot.pause()
        # `+` marks a rate multiplier pinned at its cap ("by 900%+").
        allowed = set(PACE_ARROW.values()) | set("█░%:…+")
        for bar in app.query(GaugeBar):
            drawn = bar.render().plain
            assert "◆" not in drawn
            symbols = {c for c in drawn if not c.isalnum() and not c.isspace()}
            assert symbols <= allowed, f"undocumented glyph in {drawn!r}"


class TestPaceArrow:
    """The arrow is an instruction, not a status report."""

    async def test_a_meter_burning_too_fast_is_told_to_slow_down(self, stub):
        app = FuelGaugeApp(interval=3600)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            codex = [
                b for b in app.query(GaugeBar)
                if b.gauge.percent == 98.0
            ]
            assert codex, "fixture should include an over-budget meter"
            line = codex[0].render().plain
            assert "↓" in line, f"over-budget meter must point down: {line!r}"
            assert re.search(r"by \d+%", line), f"no magnitude in {line!r}"

    async def test_the_governing_meter_carries_a_sized_instruction(self, stub):
        app = FuelGaugeApp(interval=3600)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            bars = governing_bars(app)
            assert bars, "something must govern"
            for bar in bars:
                line = bar.render().plain
                pace = bar.gauge.pace()
                assert pace.display in line, f"{pace.display!r} missing: {line!r}"


class TestOnlyTheTightestMeterSpeaks:
    """The reported bug, at the level the user sees it.

    A 5h window with headroom sitting beside a nearly-spent weekly window used
    to print "↑ by 150%" — an invitation to blow the weekly cap. Rows that do
    not constrain you now say nothing at all in that column.
    """

    async def test_non_governing_rows_carry_no_advice(self, stub):
        app = FuelGaugeApp(interval=3600)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            governing = set(map(id, governing_bars(app)))
            quiet = [b for b in app.query(GaugeBar) if id(b) not in governing]
            assert quiet, "fixture should include a meter that is not the constraint"
            for bar in quiet:
                line = bar.render().plain
                assert not re.search(r"[↑↓✗]|by \d+%|on pace|too new", line), (
                    f"slack meter should stay quiet: {line!r}"
                )

    async def test_a_spent_week_silences_an_idle_five_hour_window(self, monkeypatch):
        """The exact shape reported: plenty of burst room, no weekly budget."""
        now = datetime.now(timezone.utc)

        async def fake_fetch_all():
            return [
                ProviderSnapshot(
                    key="claude", display_name="Claude", captured_at=now,
                    gauges=[
                        Gauge("5h", "all models", 10.0, now + timedelta(hours=1),
                              window_seconds=FIVE_HOURS),
                        Gauge("7d", "all models", 96.0, now + timedelta(days=3),
                              severity="critical", window_seconds=ONE_WEEK),
                    ],
                )
            ]

        monkeypatch.setattr(app_module, "fetch_all", fake_fetch_all)
        app = FuelGaugeApp(interval=3600)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            burst, week = [b.render().plain for b in app.query(GaugeBar)]
            assert "↑" not in burst, f"5h must not offer its headroom: {burst!r}"
            assert "↓" in week, f"the week must be the one talking: {week!r}"


async def test_each_panel_reports_its_own_age(stub):
    """One global 'updated at' would hide a provider that stopped refreshing."""
    app = FuelGaugeApp(interval=3600)
    async with app.run_test() as pilot:
        await pilot.pause()
        for panel in app.query(ProviderPanel):
            assert "ago" in panel.border_subtitle or "just now" in panel.border_subtitle


async def test_refresh_updates_in_place_without_remounting(stub):
    app = FuelGaugeApp(interval=3600)
    async with app.run_test() as pilot:
        await pilot.pause()
        before = [id(b) for b in app.query(GaugeBar)]

        stub["snapshots"] = _snapshots(fable_percent=99.0)
        await pilot.press("r")
        await pilot.pause()

        after = [id(b) for b in app.query(GaugeBar)]
        assert before == after, "same-shape refresh should reuse widgets"
        fable = next(b.gauge for b in app.query(GaugeBar) if b.gauge.scope == "Fable")
        assert fable.percent == 99.0


async def test_refresh_rebuilds_when_a_scoped_limit_disappears(stub):
    app = FuelGaugeApp(interval=3600)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert len(app.query(GaugeBar)) == 4

        stub["snapshots"] = _snapshots(with_scoped=False)
        await pilot.press("r")
        await pilot.pause()

        assert len(app.query(GaugeBar)) == 3
        assert not any(b.gauge.scope == "Fable" for b in app.query(GaugeBar))


async def test_error_snapshot_shows_message_instead_of_bars(monkeypatch):
    async def fake_fetch_all():
        return [
            ProviderSnapshot(
                key="claude",
                display_name="Claude",
                captured_at=NOW,
                error="token rejected (401) — run `claude` once to refresh it",
            )
        ]

    monkeypatch.setattr(app_module, "fetch_all", fake_fetch_all)
    app = FuelGaugeApp(interval=3600)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert len(app.query(GaugeBar)) == 0
        assert "401" in str(app.query(".issue").first().render())


async def test_failed_poll_keeps_last_known_bars(stub):
    """A transient 429 must not blank the panel — that's the whole point."""
    app = FuelGaugeApp(interval=3600)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert len(app.query(GaugeBar)) == 4

        broken = ProviderSnapshot(
            key="claude",
            display_name="Claude",
            captured_at=NOW,
            error="rate limited (429) — poll less often",
        )
        stub["snapshots"] = [broken, _snapshots()[1]]
        await pilot.press("r")
        await pilot.pause()

        claude = next(p for p in app.query(ProviderPanel) if p.snapshot.key == "claude")
        assert claude.snapshot.stale is True
        assert [g.scope for g in claude.snapshot.gauges] == [
            "all models", "all models", "Fable",
        ]
        assert "429" in str(app.query(".issue").first().render())
        # bars are still on screen alongside the warning
        assert len(app.query(GaugeBar)) == 4


async def test_bar_survives_a_narrow_terminal(stub):
    app = FuelGaugeApp(interval=3600)
    async with app.run_test(size=(40, 20)) as pilot:
        await pilot.pause()
        for bar in app.query(GaugeBar):
            assert bar.render().plain  # no exception, no empty render


class TestNarrowTerminal:
    """The reset countdown must survive any width.

    It used to be the first casualty: the bar had a minimum width, so a narrow
    terminal overflowed the row and the crop took the right-hand edge — where
    the countdown lives.
    """

    RESET_TIME = re.compile(r"\d+[dhms]")

    async def _rows(self, stub, width):
        app = FuelGaugeApp(interval=3600)
        async with app.run_test(size=(width, 24)) as pilot:
            await pilot.pause()
            return [(b.render().plain, b.size.width) for b in app.query(GaugeBar)]

    @pytest.mark.parametrize("width", [120, 100, 80, 60, 50, 44, 38, 32, 26, 20, 16])
    async def test_countdown_is_always_visible(self, stub, width):
        for line, _ in await self._rows(stub, width):
            assert self.RESET_TIME.search(line), f"no reset time at width {width}: {line!r}"

    @pytest.mark.parametrize("width", [120, 100, 80, 60, 50, 44, 38, 32, 26, 20, 16])
    async def test_row_never_overflows_its_widget(self, stub, width):
        """An overflow is what silently crops the countdown."""
        for line, widget_width in await self._rows(stub, width):
            assert len(line) <= widget_width, (
                f"width {width}: rendered {len(line)} chars into {widget_width}"
            )

    async def test_bar_is_what_yields_first(self, stub):
        wide = await self._rows(stub, 100)
        narrow = await self._rows(stub, 40)
        assert "█" in wide[0][0]
        # The bar shrinks or vanishes; the numbers do not.
        assert len(narrow[0][0].replace("█", "").replace("░", "")) > 0

    async def test_percentage_survives_until_the_very_end(self, stub):
        for line, _ in await self._rows(stub, 32):
            assert "%" in line

    async def test_panel_age_survives_narrow_widths(self, stub):
        """Textual crops a subtitle from the right, where the age sits."""
        app = FuelGaugeApp(interval=3600)
        async with app.run_test(size=(34, 24)) as pilot:
            await pilot.pause()
            for panel in app.query(ProviderPanel):
                panel.refresh_titles()
            for panel in app.query(ProviderPanel):
                assert "ago" in panel.border_subtitle or "now" in panel.border_subtitle

    @pytest.mark.parametrize("width", [120, 100, 70, 60, 50, 40, 32, 26])
    async def test_legend_is_never_truncated(self, stub, width):
        """It wraps rather than ellipsizing.

        A legend cut off halfway documents some of the symbols and silently
        drops the rest, which is worse than showing none of it.
        """
        app = FuelGaugeApp(interval=3600)
        async with app.run_test(size=(width, 30)) as pilot:
            await pilot.pause()
            text = app.query_one(Legend).render().plain
            assert text.strip(), f"legend empty at width {width}"
            assert "…" not in text, f"legend truncated at width {width}"
            for arrow in PACE_ARROW.values():
                assert arrow in text, f"{arrow} undocumented at width {width}"

    @pytest.mark.parametrize("width", [120, 80, 60, 56, 54, 52])
    async def test_absolute_reset_time_is_shown_when_there_is_room(self, stub, width):
        """A duration alone means doing arithmetic against your calendar."""
        app = FuelGaugeApp(interval=3600)
        async with app.run_test(size=(width, 30)) as pilot:
            await pilot.pause()
            for bar in app.query(GaugeBar):
                line = bar.render().plain
                assert re.search(r"\d{1,2}:\d{2}", line), (
                    f"no clock time at width {width}: {line!r}"
                )

    @pytest.mark.parametrize("width", [120, 80, 60, 50, 44, 40, 36])
    async def test_the_arrow_outlives_the_reset_clock(self, stub, width):
        """What to do about a meter outranks exactly when it resets.

        Carrying the magnitude costs about six columns, and showing minutes
        alongside hours costs one more, so the wall-clock reset time yields at
        52 rather than the 44 it managed when the row was cheaper. That is the
        intended trade: the relative countdown still answers "have I got time
        for this", while nothing else on the row says which way to move.
        """
        app = FuelGaugeApp(interval=3600)
        async with app.run_test(size=(width, 30)) as pilot:
            await pilot.pause()
            for bar in governing_bars(app):
                line = bar.render().plain
                if not bar.gauge.pace().arrow:
                    continue  # no direction to point; nothing to preserve
                assert re.search(r"[↑↓✗]", line), (
                    f"no pace arrow at width {width}: {line!r}"
                )

    @pytest.mark.parametrize("width", [120, 80, 60, 50, 44])
    async def test_the_magnitude_outlives_the_reset_clock(self, stub, width):
        """"Slow down" alone is half an instruction; keep the "by how much"."""
        app = FuelGaugeApp(interval=3600)
        async with app.run_test(size=(width, 30)) as pilot:
            await pilot.pause()
            for bar in governing_bars(app):
                line = bar.render().plain
                if bar.gauge.pace().change_percent is None:
                    continue  # nothing to size: on pace, spent, or too new
                assert re.search(r"[↑↓] by \d+%", line), (
                    f"no magnitude at width {width}: {line!r}"
                )

    @pytest.mark.parametrize("width", [120, 80, 60, 52, 46])
    async def test_columns_line_up_across_both_boxes(self, stub, width):
        """A jagged table reads as a broken one.

        Rows used to choose their layout independently, which was invisible
        while every tail was the same width. The moment the pace column carried
        a magnitude it stopped being: a short row had eight columns spare that a
        row reading "↓ by 92%" did not, so it picked a richer variant and its
        bar started somewhere else entirely.
        """
        app = FuelGaugeApp(interval=3600)
        async with app.run_test(size=(width, 30)) as pilot:
            await pilot.pause()
            bars = list(app.query(GaugeBar))
            lines = [b.render().plain for b in bars]
            starts = {line.index("%") for line in lines}
            assert len(starts) == 1, (
                f"percent column jagged at width {width}: {lines}"
            )
            # The pace column holds arrows and words alike, so anchor on where
            # each row's own pace text lands rather than on a glyph.
            pace_starts = {
                line.rindex(display)
                for bar, line in zip(bars, lines)
                if (display := bar.gauge.pace().display) and display in line
            }
            assert len(pace_starts) <= 1, (
                f"pace column jagged at width {width}: {lines}"
            )

    async def test_legend_grows_instead_of_truncating(self, stub):
        """Vertical space below the panels is free; horizontal space is not."""
        heights = {}
        for width in (120, 50):
            app = FuelGaugeApp(interval=3600)
            async with app.run_test(size=(width, 30)) as pilot:
                await pilot.pause()
                heights[width] = app.query_one(Legend).size.height
        assert heights[50] > heights[120]

    async def test_legend_sits_below_both_panels(self, stub):
        """One key for both boxes, not a caption on whichever it sits beside."""
        app = FuelGaugeApp(interval=3600)
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause()
            legend_top = app.query_one(Legend).region.y
            for panel in app.query(ProviderPanel):
                assert panel.region.y < legend_top
