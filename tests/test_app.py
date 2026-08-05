"""Headless tests that drive the real Textual app with the network stubbed out."""

from datetime import datetime, timedelta, timezone

import pytest

from agents_fuel_gauge import app as app_module
from agents_fuel_gauge.app import FuelGaugeApp, GaugeBar, ProviderPanel, StatusLine
from agents_fuel_gauge.models import Gauge, ProviderSnapshot

NOW = datetime.now(timezone.utc)


def _snapshots(fable_percent: float = 91.0, with_scoped: bool = True):
    claude = ProviderSnapshot(
        key="claude",
        display_name="Claude",
        plan="Max 20x",
        account="a•••@e•••.com",
        captured_at=NOW,
        gauges=[
            Gauge("5h", "all models", 5.0, NOW + timedelta(hours=1)),
            Gauge("7d", "all models", 50.0, NOW + timedelta(hours=18)),
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
                runs_out_first=True,
            )
        )
    codex = ProviderSnapshot(
        key="codex",
        display_name="Codex",
        plan="Pro",
        captured_at=NOW,
        gauges=[
            Gauge("7d", "all models", 98.0, NOW + timedelta(days=2), "critical", True)
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
        assert widget.gauge.runs_out_first is True

        drawn = widget.render().plain
        assert "7d Fable" in drawn
        assert "91%" in drawn
        assert "◆" in drawn, "the first-to-run-out gauge gets a marker"


async def test_status_line_names_what_runs_out_first(stub):
    app = FuelGaugeApp(interval=3600)
    async with app.run_test() as pilot:
        await pilot.pause()
        # Codex 98% outranks Fable 91%; both are flagged, the hotter one wins.
        text = app.query(StatusLine).first().render().plain
        assert "Codex" in text and "98" in text
        # Plain wording, not the optimization-theory term it started as.
        assert "runs out first" in text
        assert "binding" not in text.lower()


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
        # ...and the status line agrees rather than reporting "no usage data"
        assert "Codex" in app.query(StatusLine).first().render().plain


async def test_bar_survives_a_narrow_terminal(stub):
    app = FuelGaugeApp(interval=3600)
    async with app.run_test(size=(40, 20)) as pilot:
        await pilot.pause()
        for bar in app.query(GaugeBar):
            assert bar.render().plain  # no exception, no empty render
