"""Headless tests that drive the real Textual app with the network stubbed out."""

import re
from datetime import datetime, timedelta, timezone

import pytest

from agents_fuel_gauge import app as app_module, history
from agents_fuel_gauge.app import (
    FuelGaugeApp,
    GaugeBar,
    HistorySegment,
    HistoryLegend,
    Legend,
    ProviderHistory,
    ProviderPanel,
    ResponsiveFooter,
    UsageHistoryPlot,
    _axis_label,
    _history_viewport,
    _segment_viewport,
)
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


def _quantized_samples(regimes, sample_seconds=10 * 60):
    """Mirror the integer-percent staircase the live providers expose."""
    total_seconds = sum(days * 86_400 for days, _ in regimes)
    starts_at = NOW.timestamp() - total_seconds
    samples = []
    for elapsed in range(0, int(total_seconds) + 1, sample_seconds):
        used = 0.0
        regime_start = 0.0
        for days, rate in regimes:
            regime_end = regime_start + days * 86_400
            used += (
                max(0.0, min(elapsed, regime_end) - regime_start)
                * rate
                / 86_400
            )
            regime_start = regime_end
        samples.append({"t": starts_at + elapsed, "pct": float(int(used))})
    return samples


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

    async def fake_fetch_all(max_age=60.0):
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


async def test_an_uninstalled_provider_has_no_tui_panel():
    """A product the user does not have should consume no phone-screen space."""
    absent = ProviderSnapshot(
        key="claude", display_name="Claude", installed=False,
        error="Claude CLI is not installed or not on PATH",
    )

    async def one_provider():
        return [absent, _snapshots()[1]]

    app = FuelGaugeApp(interval=3600, fetcher=one_provider)
    async with app.run_test() as pilot:
        await pilot.pause()
        panels = list(app.query(ProviderPanel))
        assert [panel.snapshot.key for panel in panels] == ["codex"]
        assert [snapshot.key for snapshot in app.snapshots] == ["codex"]
        assert not app.query("#panel-claude")


async def test_no_installed_providers_gets_one_generic_empty_state():
    """An empty dashboard should explain itself without drawing two errors."""
    async def no_providers():
        return [
            ProviderSnapshot(
                key=key, display_name=name, installed=False,
                error=f"{name} CLI is not installed or not on PATH",
            )
            for key, name in (("claude", "Claude"), ("codex", "Codex"))
        ]

    app = FuelGaugeApp(interval=3600, fetcher=no_providers)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert not app.query(ProviderPanel)
        empty = app.query_one("#no-providers")
        assert "no supported agent CLIs" in empty.render().plain
        assert app.query_one(Legend).display is False
        assert app.query_one(HistoryLegend).display is False

        await pilot.press("h")
        await pilot.pause()
        assert app.query_one("#panels").display is True
        assert app.query_one("#plots").display is False


def test_history_navigation_bindings_are_registered_for_the_footer():
    """Discoverability matters for a pane with no always-visible tab."""
    assert ("h", "toggle_history", "History") in FuelGaugeApp.BINDINGS
    assert ("z", "toggle_history_zoom", "Zoom") in FuelGaugeApp.BINDINGS
    assert ("d", "set_history_mode('details')", "Details") in FuelGaugeApp.BINDINGS
    assert ("d", "set_history_mode('overview')", "Overview") in FuelGaugeApp.BINDINGS
    assert ("o", "command_palette", "Options") in FuelGaugeApp.BINDINGS
    assert not any(key == "p" for key, _, _ in FuelGaugeApp.BINDINGS)
    assert not any(key == "ctrl+p" for key, _, _ in FuelGaugeApp.BINDINGS)


async def test_theme_footer_names_the_action_and_options_opens_the_palette():
    async def no_providers():
        return []

    app = FuelGaugeApp(interval=3600, fetcher=no_providers)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        footer = app.query_one(ResponsiveFooter)
        footer_text = " ".join(child.render().plain for child in footer.children)
        assert app.theme == "textual-dark"
        assert app.active_bindings["t"].binding.description == "Light"
        assert app.active_bindings["o"].binding.description == "Options"
        assert "Light" in footer_text and "Options" in footer_text

        await pilot.press("t")
        await pilot.pause()
        footer_text = " ".join(child.render().plain for child in footer.children)
        assert app.theme == "textual-light"
        assert app.active_bindings["t"].binding.description == "Dark"
        assert "Dark" in footer_text and "Light" not in footer_text

        await pilot.press("o")
        await pilot.pause()
        assert app.screen.id == "--command-palette"


async def test_footer_wraps_all_clickable_actions_onto_two_rows():
    async def no_providers():
        return []

    app = FuelGaugeApp(interval=3600, fetcher=no_providers)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        footer = app.query_one(ResponsiveFooter)
        assert len(footer.children) == 5
        assert all("Zoom" not in child.render().plain for child in footer.children)
        assert not footer.has_class("-wrapped")
        assert len({child.region.y for child in footer.children}) == 1

        await pilot.resize_terminal(40, 24)
        await pilot.pause()
        assert footer.has_class("-wrapped")
        assert footer.size.height == 2
        assert len(footer.children) == 5
        assert len({child.region.y for child in footer.children}) == 2


def test_detailed_history_viewport_focuses_the_recorded_tail():
    """A young trace near reset should not spend a phone screen on empty days."""
    reset = NOW + timedelta(hours=1)
    gauge = Gauge(
        "7d", "all models", 95, reset, window_seconds=ONE_WEEK
    )
    samples = [
        {"t": (NOW - timedelta(hours=2)).timestamp(), "pct": 94.0},
        {"t": NOW.timestamp(), "pct": 95.0},
    ]

    x_min, x_max, y_min, y_max = _history_viewport(gauge, samples, False)

    opened = reset.timestamp() - ONE_WEEK
    assert x_min > opened + 6 * 86_400
    assert NOW.timestamp() < x_max < reset.timestamp()
    assert x_max - NOW.timestamp() < 10 * 60
    assert 90 < y_min < 95
    assert y_max < 100


def test_detailed_history_viewport_crops_unrecorded_future_for_early_trace():
    """A new Codex window should not reserve most of the graph for next week."""
    reset = NOW + timedelta(days=6)
    gauge = Gauge("7d", "all models", 12, reset, window_seconds=ONE_WEEK)
    samples = [
        {"t": (NOW - timedelta(hours=2)).timestamp(), "pct": 10.0},
        {"t": NOW.timestamp(), "pct": 12.0},
    ]

    x_min, x_max, y_min, y_max = _history_viewport(gauge, samples, False)

    assert x_min < samples[0]["t"]
    assert NOW.timestamp() < x_max < (NOW + timedelta(hours=1)).timestamp()
    assert reset.timestamp() - x_max > 5 * 86_400
    assert 0 < y_min < y_max < 20


def test_full_history_viewport_retains_the_original_context():
    reset = NOW + timedelta(hours=1)
    gauge = Gauge(
        "7d", "all models", 95, reset, window_seconds=ONE_WEEK
    )
    samples = [
        {"t": (NOW - timedelta(hours=2)).timestamp(), "pct": 94.0},
        {"t": NOW.timestamp(), "pct": 95.0},
    ]

    x_min, x_max, y_min, y_max = _history_viewport(gauge, samples, True)

    assert x_min == reset.timestamp() - ONE_WEEK
    assert x_max == reset.timestamp()
    assert (y_min, y_max) == (0, 100)


def test_segment_viewport_is_tighter_than_the_recorded_overview():
    samples = _quantized_samples([(2, 20), (4, 6)])
    inferred = history.segments(samples)

    x_min, x_max, y_min, y_max = _segment_viewport(samples, inferred[-1])

    assert samples[0]["t"] < x_min < inferred[-1].start
    assert inferred[-1].end <= x_max <= samples[-1]["t"]
    assert y_min < y_max
    assert y_max - y_min < samples[-1]["pct"] - samples[0]["pct"]


def test_zoomed_axis_labels_follow_the_visible_span_not_the_quota_window():
    """Three hours at the end of a week needs times, not one date repeated."""
    timestamp = NOW.timestamp()
    assert ":" in _axis_label(timestamp, 3 * 3_600)
    assert ":" in _axis_label(timestamp, 36 * 3_600)
    assert ":" not in _axis_label(timestamp, ONE_WEEK)


def test_plot_severity_colors_are_names_plotext_actually_inks():
    """Plotext drops unknown colour names silently instead of raising.

    The Rich name "yellow" is one it drops, so a warning-severity trace —
    the severity most worth signalling — rendered in the widget theme's
    accent instead. Building a real figure per name pins the whole failure
    mode, not just the one name that bit us.
    """
    import plotext

    assert set(app_module.PLOTEXT_SEVERITY) == set(app_module.SEVERITY_COLOR)
    for name in app_module.PLOTEXT_SEVERITY.values():
        plotext.clear_figure()
        plotext.plot_size(24, 6)
        plotext.theme("clear")  # no chrome colours: any ink must be the series
        plotext.plot([0, 1], [0, 1], color=name)
        assert "\x1b[38;5;" in plotext.build()
    plotext.clear_figure()


async def test_history_key_switches_views_and_mounts_provider_plots(
    stub, tmp_path, monkeypatch
):
    """The switched view keeps the existing dashboard layout undisturbed."""
    monkeypatch.setenv("AFG_CACHE_DIR", str(tmp_path))
    for snapshot in stub["snapshots"]:
        gauge = snapshot.tightest
        history.append_sample(
            snapshot.key, gauge.label, gauge.percent - 1, NOW - timedelta(hours=1)
        )
        history.append_sample(snapshot.key, gauge.label, gauge.percent, NOW)

    app = FuelGaugeApp(interval=3600)
    async with app.run_test(size=(100, 36)) as pilot:
        await pilot.pause()
        plots = app.query_one("#plots")
        footer = app.query_one(ResponsiveFooter)
        assert plots.display is False
        assert "z" not in app.active_bindings
        assert "d" not in app.active_bindings
        assert all("Zoom" not in child.render().plain for child in footer.children)

        await pilot.press("h")
        await pilot.pause()

        assert plots.display is True
        assert app.active_bindings["z"].binding.description == "Zoom"
        assert app.active_bindings["d"].binding.description == "Details"
        assert any("Zoom" in child.render().plain for child in footer.children)
        assert app.query_one("#panels").display is False
        history_legend = app.query_one(HistoryLegend)
        assert history_legend.display is True
        legend_text = history_legend.render().plain
        for meaning in (
            "normal usage",
            "warning usage",
            "critical usage",
            "ideal budget pace",
            "required path to 100% at reset",
        ):
            assert meaning in legend_text
        assert len(app.query(ProviderHistory)) == 2
        assert len(app.query(UsageHistoryPlot)) == 2
        rates = app.query(".history-rate")
        assert len(rates) == 2
        assert all(
            "not enough movement" in str(line.render()) for line in rates
        )

        histories = list(app.query(ProviderHistory))
        assert all(item.border_subtitle == "recorded" for item in histories)
        await pilot.press("z")
        await pilot.pause()
        assert app.history_full_window is True
        assert all(
            item.border_subtitle == "full window"
            for item in app.query(ProviderHistory)
        )

        await pilot.press("d")
        await pilot.pause()
        assert app.history_details is True
        assert "z" not in app.active_bindings
        assert app.active_bindings["d"].binding.description == "Overview"
        assert not app.query(UsageHistoryPlot)
        assert all(
            "not enough movement to split into details"
            in item.render().plain
            for item in app.query(".history-empty")
        )

        await pilot.press("d")
        await pilot.pause()
        assert app.history_details is False
        assert app.active_bindings["d"].binding.description == "Details"
        assert "z" in app.active_bindings
        assert all(
            item.border_subtitle == "full window"
            for item in app.query(ProviderHistory)
        )

        await pilot.press("h")
        await pilot.pause()
        assert plots.display is False
        assert app.query_one("#panels").display is True
        assert history_legend.display is False
        assert "z" not in app.active_bindings
        assert "d" not in app.active_bindings
        assert all("Zoom" not in child.render().plain for child in footer.children)


async def test_details_stacks_inferred_segments_newest_first(monkeypatch):
    samples = _quantized_samples([(2, 20), (4, 6)])
    inferred = history.segments(samples)
    snapshot = ProviderSnapshot(
        key="claude",
        display_name="Claude",
        captured_at=NOW,
        gauges=[
            Gauge(
                "9d",
                "all models",
                samples[-1]["pct"],
                NOW + timedelta(days=3),
                window_seconds=9 * 86_400,
            )
        ],
    )

    async def fake_fetch_all():
        return [snapshot]

    monkeypatch.setattr(app_module, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(history, "read_window", lambda *args: samples)
    app = FuelGaugeApp(interval=3600)
    async with app.run_test(size=(60, 36)) as pilot:
        await pilot.pause()
        await pilot.press("h")
        await pilot.press("d")
        await pilot.pause()

        cards = list(app.query(HistorySegment))
        assert len(cards) == len(inferred) == 2
        assert [card.segment for card in cards] == list(reversed(inferred))
        assert [card.position for card in cards] == [0, 1]
        summaries = [
            card.query_one(".history-segment-summary").render().plain
            for card in cards
        ]
        assert f"{inferred[-1].delta_pct:+.1f}%" in summaries[0]
        assert f"fitted {inferred[-1].rate_per_day:.1f}%/d" in summaries[0]
        assert "over" in summaries[0]
        assert app.query_one(ProviderHistory).border_subtitle == "details · 2 segments"
        assert app.active_bindings["d"].binding.description == "Overview"
        assert "z" not in app.active_bindings
        legend = app.query_one(HistoryLegend).render().plain
        assert "fitted segment rate" in legend
        assert "ideal budget pace" not in legend
        assert "<svg" in app.export_screenshot()


async def test_history_readout_chains_regimes_and_judges_only_the_latest(
    monkeypatch,
):
    """Old usage is context; only the rate the user can still change gets advice."""
    samples = _quantized_samples([(2, 20), (4, 6)])
    snapshot = ProviderSnapshot(
        key="claude",
        display_name="Claude",
        captured_at=NOW,
        gauges=[
            Gauge(
                "9d",
                "all models",
                samples[-1]["pct"],
                NOW + timedelta(days=3),
                window_seconds=9 * 86_400,
            )
        ],
    )

    async def fake_fetch_all():
        return [snapshot]

    monkeypatch.setattr(app_module, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(history, "read_window", lambda *args: samples)
    app = FuelGaugeApp(interval=3600)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        await pilot.press("h")
        await pilot.pause()

        line = app.query_one(".history-rate").render().plain
        assert re.search(r"20\.\d%/d", line)
        assert " → " in line
        assert "6.0%/d" in line
        assert line.count("↑") == 1
        assert "↓" not in line
        assert line.endswith("↑ · required 12.0%/d")


async def test_history_readout_leaves_a_steady_on_pace_regime_unmarked(
    monkeypatch,
):
    """A rate inside the pace band needs context but no instruction arrow."""
    samples = _quantized_samples([(4, 14.3)])
    snapshot = ProviderSnapshot(
        key="claude",
        display_name="Claude",
        captured_at=NOW,
        gauges=[
            Gauge(
                "7d",
                "all models",
                samples[-1]["pct"],
                NOW + timedelta(days=3),
                window_seconds=ONE_WEEK,
            )
        ],
    )

    async def fake_fetch_all():
        return [snapshot]

    monkeypatch.setattr(app_module, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(history, "read_window", lambda *args: samples)
    app = FuelGaugeApp(interval=3600)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        await pilot.press("h")
        await pilot.pause()

        line = app.query_one(".history-rate").render().plain
        assert " → " not in line
        assert re.search(r"14\.\d%/d", line)
        assert "↑" not in line and "↓" not in line
        assert line.endswith("· required 14.3%/d")


async def test_both_history_ranges_render_at_phone_width(monkeypatch):
    """Recorded is the phone default; full context remains a safe escape hatch."""
    samples = _quantized_samples([(0.25, 20)])
    snapshot = ProviderSnapshot(
        key="claude",
        display_name="Claude",
        captured_at=NOW,
        gauges=[
            Gauge(
                "7d", "all models", samples[-1]["pct"],
                NOW + timedelta(hours=2), window_seconds=ONE_WEEK,
            )
        ],
    )

    async def fake_fetch_all():
        return [snapshot]

    monkeypatch.setattr(app_module, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(history, "read_window", lambda *args: samples)
    app = FuelGaugeApp(interval=3600)
    async with app.run_test(size=(42, 24)) as pilot:
        await pilot.pause()
        await pilot.press("h")
        await pilot.pause()
        assert app.query_one(ProviderHistory).border_subtitle == "recorded"
        assert "<svg" in app.export_screenshot()

        await pilot.press("z")
        await pilot.pause()
        assert app.query_one(ProviderHistory).border_subtitle == "full window"
        assert "<svg" in app.export_screenshot()


async def test_history_view_explains_that_samples_have_not_accrued(
    stub, tmp_path, monkeypatch
):
    """An empty chart should read as young data, not a rendering failure."""
    monkeypatch.setenv("AFG_CACHE_DIR", str(tmp_path))
    app = FuelGaugeApp(interval=3600)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("h")
        await pilot.pause()

        placeholders = app.query(".history-empty")
        assert len(placeholders) == 2
        assert all("samples accrue" in str(item.render()) for item in placeholders)


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


async def test_each_masked_account_survives_the_first_sized_title_refresh(stub):
    """The one-second age tick must not erase an account shown at mount."""
    app = FuelGaugeApp(interval=3600)
    async with app.run_test(size=(40, 24)) as pilot:
        await pilot.pause()
        account_panels = [
            panel for panel in app.query(ProviderPanel) if panel.snapshot.account
        ]
        assert account_panels
        for panel in account_panels:
            account = panel.snapshot.account
            assert account is not None  # narrowed by account_panels
            assert account in panel.border_subtitle
            panel.refresh_titles()  # exactly what the first one-second tick does
            assert account in panel.border_subtitle


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


async def test_refresh_key_bypasses_the_normal_cache(monkeypatch):
    requested_ages = []

    async def fake_fetch_all(max_age=60.0):
        requested_ages.append(max_age)
        return _snapshots()

    monkeypatch.setattr(app_module, "fetch_all", fake_fetch_all)
    app = FuelGaugeApp(interval=3600)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("r")
        await pilot.pause()

    assert requested_ages == [60.0, 0]


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
