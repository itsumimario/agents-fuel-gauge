"""Output-mode tests.

`--check` output is a documented interface (the README shows people piping it
through awk), so its shape is pinned here rather than left to drift.
"""

import json

import pytest

from agents_fuel_gauge.cli import main, render_plain, render_pretty
from agents_fuel_gauge.demo import demo_snapshots
from agents_fuel_gauge.models import ProviderSnapshot


class TestPlain:
    def test_one_row_per_gauge_with_exactly_seven_whitespace_free_columns(self):
        """Any field containing a space would shift every awk column after it."""
        out = render_plain(demo_snapshots())
        rows = [r for r in out.splitlines() if r.strip()]
        assert len(rows) == 5
        for row in rows:
            assert len(row.split()) == 7, row

    def test_pace_column_is_appended_not_inserted(self):
        """Columns 1-6 are a published interface; pace must not shift them."""
        rows = [r.split() for r in render_plain(demo_snapshots()).splitlines() if r.strip()]
        for cells in rows:
            assert cells[0] in {"claude", "codex"}      # $1 provider
            assert cells[1].endswith(("h", "d"))         # $2 window
            assert cells[3].endswith("%")                # $4 used
            assert cells[5] in {"-", "FIRST", "STALE", "FIRST,STALE"}  # $6 flags
            assert cells[6] in {                          # $7 pace, new
                "-", "slow_down", "on_track", "spare_capacity",
                "exhausted", "too_early",
            }

    def test_marks_what_runs_out_first_without_symbols(self):
        out = render_plain(demo_snapshots())
        first = [r for r in out.splitlines() if "FIRST" in r]
        assert len(first) == 2  # one per provider
        assert "◆" not in out, "plain output must stay ASCII-safe"

    def test_no_ansi_escapes(self):
        assert "\033" not in render_plain(demo_snapshots())

    def test_percent_column_is_awk_friendly(self):
        """The README documents `awk '$4+0 > 80'`; keep that column numeric."""
        rows = [r.split() for r in render_plain(demo_snapshots()).splitlines() if r.strip()]
        overs = [r for r in rows if float(r[3].rstrip("%")) > 80]
        assert len(overs) == 2

    def test_error_row_is_labelled(self):
        snaps = [ProviderSnapshot(key="claude", display_name="Claude", error="nope")]
        assert "ERROR" in render_plain(snaps)

    def test_empty_input_says_so(self):
        assert "no usage data" in render_plain([])


class TestPretty:
    def test_draws_bars_and_a_legend(self):
        out = render_pretty(demo_snapshots(), color=False)
        assert "█" in out and "░" in out
        assert "◆ runs out before the others" in out

    def test_no_color_means_no_escapes(self):
        assert "\033" not in render_pretty(demo_snapshots(), color=False)

    def test_color_mode_emits_escapes(self):
        assert "\033" in render_pretty(demo_snapshots(), color=True)


class TestJson:
    """The output is an envelope: {at, directive, providers}.

    A subscriber steering its own rate wants one instruction, not a table it
    has to reduce itself, so the directive is hoisted to the top level.
    """

    def test_envelope_shape(self, capsys):
        assert main(["--demo", "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert set(payload) == {"at", "directive", "providers"}
        assert [p["provider"] for p in payload["providers"]] == ["claude", "codex"]

    def test_gauge_fields(self, capsys):
        main(["--demo", "--json"])
        payload = json.loads(capsys.readouterr().out)
        gauge = payload["providers"][0]["gauges"][0]
        assert set(gauge) == {
            "window", "scope", "label", "percent", "severity",
            "runsOutFirst", "resetsAt", "secondsRemaining",
            "windowSeconds", "pace",
        }

    def test_seconds_remaining_is_precomputed(self, capsys):
        main(["--demo", "--json"])
        payload = json.loads(capsys.readouterr().out)
        assert all(
            g["secondsRemaining"] > 0
            for p in payload["providers"] for g in p["gauges"]
        )

    def test_pace_fields(self, capsys):
        main(["--demo", "--json"])
        payload = json.loads(capsys.readouterr().out)
        pace = payload["providers"][0]["gauges"][0]["pace"]
        assert set(pace) == {
            "verdict", "ratio", "projectedUsagePercent", "rateAdjustment",
            "elapsedPercent", "exhaustsInSeconds", "exhaustsBeforeReset",
            "advice",
        }


class TestDirective:
    """The signal a downstream service subscribes to."""

    def _directive(self, capsys):
        main(["--demo", "--json"])
        return json.loads(capsys.readouterr().out)["directive"]

    def test_carries_a_usable_rate_multiplier(self, capsys):
        directive = self._directive(capsys)
        assert isinstance(directive["rateAdjustment"], float)
        assert 0.0 <= directive["rateAdjustment"] <= 10.0

    def test_names_the_constraint_it_came_from(self, capsys):
        directive = self._directive(capsys)
        assert directive["constraint"]["provider"] in {"claude", "codex"}
        assert directive["constraint"]["label"]

    def test_picks_the_worst_pace_not_the_fullest_bar(self, capsys):
        """82% with 5 days left outranks 91% with 18 hours left."""
        directive = self._directive(capsys)
        assert directive["verdict"] == "slow_down"
        assert directive["constraint"]["provider"] == "codex"
        assert directive["constraint"]["percent"] == 82.0

    def test_ignores_windows_too_new_to_judge(self, capsys):
        """A window minutes old produces a wild ratio; it must not win."""
        directive = self._directive(capsys)
        assert "Spark" not in directive["constraint"]["label"]


class TestExitCodes:
    def test_success_is_zero(self, capsys):
        assert main(["--demo", "--check"]) == 0
        capsys.readouterr()

    def test_failure_is_non_zero(self, monkeypatch, capsys):
        async def broken():
            return [ProviderSnapshot(key="claude", display_name="Claude", error="nope")]

        monkeypatch.setattr("agents_fuel_gauge.cli.fetch_all", broken)
        assert main(["--check"]) == 1
        capsys.readouterr()


class TestDemoMode:
    def test_demo_never_touches_the_network(self, monkeypatch, capsys):
        def explode():
            raise AssertionError("--demo must not hit the network")

        monkeypatch.setattr("agents_fuel_gauge.cli.fetch_all", explode)
        assert main(["--demo", "--check"]) == 0
        assert "Fable" in capsys.readouterr().out

    def test_demo_covers_every_severity(self):
        severities = {g.severity for s in demo_snapshots() for g in s.gauges}
        assert severities == {"normal", "warning", "critical"}

    def test_demo_carries_no_real_account_data(self):
        """Screenshots are generated from this; it must stay synthetic."""
        for snap in demo_snapshots():
            assert snap.account == "d•••@e•••.com"


@pytest.mark.parametrize("flag", ["--check", "--json", "--demo"])
def test_flags_parse(flag, capsys):
    main([flag, "--demo"] if flag != "--demo" else [flag, "--check"])
    capsys.readouterr()
