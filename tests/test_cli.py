"""Output-mode tests.

`--check` output is a documented interface (the README shows people piping it
through awk), so its shape is pinned here rather than left to drift.
"""

import json
import re
from datetime import datetime, timedelta, timezone

import pytest

from agents_fuel_gauge.cli import build_payload, main, render_plain, render_pretty
from agents_fuel_gauge.demo import demo_snapshots
from agents_fuel_gauge.models import Gauge, ProviderSnapshot


class TestPlain:
    def test_one_row_per_gauge_with_whitespace_free_columns(self):
        """Any field containing a space would shift every awk column after it."""
        out = render_plain(demo_snapshots())
        rows = [r for r in out.splitlines() if r.strip()]
        assert len(rows) == 5
        for row in rows:
            assert len(row.split()) == 10, row

    def test_new_columns_are_appended_not_inserted(self):
        """Columns 1-6 are a published interface; additions must not shift them."""
        rows = [r.split() for r in render_plain(demo_snapshots()).splitlines() if r.strip()]
        for cells in rows:
            assert cells[0] in {"claude", "codex"}      # $1 provider
            assert cells[1].endswith(("h", "d"))         # $2 window
            assert cells[3].endswith("%")                # $4 used
            # $6 flags: a comma-joined set, so new flags never shift a column.
            assert set(cells[5].split(",")) <= {
                "-", "ACTIVE", "GOVERNS", "STALE",
            }, cells[5]
            assert cells[6] in {                          # $7 pace verdict
                "-", "slow_down", "on_track", "spare_capacity",
                "exhausted", "too_early",
            }

    def test_change_column_is_signed_and_awk_friendly(self):
        """`awk '$10+0 < -50'` must select the meters needing real throttling."""
        rows = [r.split() for r in render_plain(demo_snapshots()).splitlines() if r.strip()]
        for cells in rows:
            cell = cells[9]
            assert cell == "-" or cell[0] in "+-", cell
        throttle = [r for r in rows if r[9] != "-" and float(r[9].strip("+-%")) and r[9][0] == "-"]
        assert throttle, "demo data should include a meter told to slow down"

    def test_sign_matches_the_verdict(self):
        """A negative change and a `spare_capacity` verdict would contradict."""
        rows = [r.split() for r in render_plain(demo_snapshots()).splitlines() if r.strip()]
        for cells in rows:
            if cells[9] == "-":
                continue
            if cells[6] == "slow_down":
                assert cells[9].startswith("-"), cells
            if cells[6] == "spare_capacity":
                assert cells[9].startswith("+"), cells

    def test_marks_what_active_limit_without_symbols(self):
        out = render_plain(demo_snapshots())
        flagged = [r for r in out.splitlines() if "ACTIVE" in r]
        # Only Claude reports an active limit; Codex reports none, so we
        # invent none.
        assert len(flagged) == 1
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

    def test_an_absent_cli_has_a_machine_distinct_status(self):
        snap = ProviderSnapshot(
            key="claude", display_name="Claude", installed=False,
            error="Claude CLI is not installed or not on PATH",
        )
        out = render_plain([snap])
        assert "NOT_INSTALLED" in out
        assert "ERROR" not in out

    def test_empty_input_says_so(self):
        assert "no usage data" in render_plain([])


class TestPretty:
    def test_draws_bars_and_a_legend(self):
        out = render_pretty(demo_snapshots(), color=False)
        assert "█" in out and "░" in out
        assert "average rate so far" in out

    def test_every_arrow_it_can_draw_is_in_the_legend(self):
        """Same rule as the TUI: no glyph without a key on the same screen."""
        out = render_pretty(demo_snapshots(), color=False)
        legend = out.rsplit("\n\n", 1)[-1]
        for arrow in ("↓", "↑", "✗"):
            assert arrow in legend

    def test_states_without_a_direction_are_words_needing_no_key(self):
        """A lone meter governs by default, so its wording is what shows."""
        now = datetime.now(timezone.utc)
        snap = ProviderSnapshot(
            key="claude", display_name="Claude", captured_at=now,
            gauges=[
                Gauge("7d", "all models", 50.0, now + timedelta(days=3, hours=12),
                      window_seconds=7 * 86_400),
            ],
        )
        out = render_pretty([snap], color=False)
        assert "on pace" in out
        # Only the gauge rows: `·` is also the subtitle separator.
        rows = [line for line in out.splitlines() if "█" in line or "░" in line]
        assert rows
        for line in rows:
            assert "·" not in line and "◦" not in line, line

    def test_rows_carry_the_magnitude_not_just_the_direction(self):
        """"Slow down" without "by how much" is half an instruction."""
        rows = [
            line for line in render_pretty(demo_snapshots(), color=False).splitlines()
            if "█" in line or "░" in line
        ]
        assert rows
        assert any(re.search(r"[↑↓] by \d+%", line) for line in rows)

    def test_no_prose_advice_block(self):
        """Advice now rides on the row it applies to, not in a paragraph."""
        out = render_pretty(demo_snapshots(), color=False)
        assert "this meter" not in out

    def test_no_color_means_no_escapes(self):
        assert "\033" not in render_pretty(demo_snapshots(), color=False)

    def test_color_mode_emits_escapes(self):
        assert "\033" in render_pretty(demo_snapshots(), color=True)

    def test_an_absent_cli_is_explicit_in_pretty_output(self):
        snap = ProviderSnapshot(
            key="claude", display_name="Claude", installed=False,
            error="Claude CLI is not installed or not on PATH",
        )
        assert "NOT INSTALLED" in render_pretty([snap], color=False)


class TestJson:
    """The output is an envelope: {at, directive, providers}.

    A subscriber steering its own rate wants one instruction, not a table it
    has to reduce itself, so the directive is hoisted to the top level.
    """

    def test_envelope_shape(self, capsys):
        assert main(["--demo", "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert set(payload) == {"at", "directives", "providers"}
        assert [p["provider"] for p in payload["providers"]] == ["claude", "codex"]

    def test_gauge_fields(self, capsys):
        main(["--demo", "--json"])
        payload = json.loads(capsys.readouterr().out)
        gauge = payload["providers"][0]["gauges"][0]
        assert set(gauge) == {
            "window", "scope", "label", "percent", "severity",
            "activeLimit", "resetsAt", "secondsRemaining",
            "windowSeconds", "pace",
        }

    def test_provider_records_expose_installation_separately_from_errors(self):
        absent = ProviderSnapshot(
            key="claude", display_name="Claude", installed=False,
            error="Claude CLI is not installed or not on PATH",
        )
        payload = build_payload([absent], "2026-08-13T12:00:00+00:00")
        provider = payload["providers"][0]
        assert provider["installed"] is False
        assert "not installed" in provider["error"]
        assert payload["directives"] == []

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
            "verdict", "direction", "ratio", "projectedUsagePercent",
            "rateAdjustment", "changePercent", "elapsedPercent",
            "exhaustsInSeconds", "exhaustsBeforeReset", "advice",
        }


class TestDirectives:
    """What a downstream service subscribes to: one entry per meter."""

    def _rows(self, capsys):
        main(["--demo", "--json"])
        return json.loads(capsys.readouterr().out)["directives"]

    def test_one_entry_per_meter(self, capsys):
        assert len(self._rows(capsys)) == 5

    def test_every_entry_names_its_provider_and_meter(self, capsys):
        for row in self._rows(capsys):
            assert row["provider"] in {"claude", "codex"}
            assert row["label"]

    def test_each_carries_its_own_rate_multiplier(self, capsys):
        for row in self._rows(capsys):
            assert 0.0 <= row["rateAdjustment"] <= 10.0

    def test_each_carries_a_signed_change_matching_its_direction(self, capsys):
        """The same instruction the arrow draws, in a form a service can act on."""
        for row in self._rows(capsys):
            if row["changePercent"] is None:
                # No magnitude exists for these: nothing to scale (exhausted)
                # or nothing worth quantifying (on budget, too new, capped).
                assert row["verdict"] in (
                    "exhausted", "on_track", "too_early", "spare_capacity",
                )
                continue
            if row["direction"] == "down":
                assert row["changePercent"] < 0
            else:
                assert row["changePercent"] > 0

    def test_each_carries_its_own_reset_time(self, capsys):
        for row in self._rows(capsys):
            assert row["resetsAt"]
            assert row["secondsRemaining"] > 0

    def test_each_says_whether_it_actually_governs(self, capsys):
        rows = self._rows(capsys)
        assert any(r["governs"] for r in rows)
        assert any(not r["governs"] for r in rows), (
            "demo should include a meter whose slack another meter overrules"
        )

    def test_the_effective_multiplier_never_exceeds_the_meters_own(self, capsys):
        """A subscriber scaling on this must not be told it can go faster.

        Only meaningful for meters with a real reading: `too_early` reports
        `rateAdjustment: 1.0` as a deliberate no-opinion placeholder, and the
        effective figure rightly comes from the meters that do have one.
        """
        for row in self._rows(capsys):
            if row["verdict"] == "too_early":
                continue
            assert row["effectiveRateAdjustment"] <= row["rateAdjustment"] + 1e-9

    def test_an_unjudgeable_meter_still_reports_the_real_constraint(self, capsys):
        """A young meter must never turn uncertainty into permission."""
        for row in self._rows(capsys):
            if row["verdict"] == "too_early":
                assert row["rateAdjustment"] == 1.0
                assert row["effectiveRateAdjustment"] <= 1.0

    def test_a_non_governing_meter_reports_what_holds_it(self, capsys):
        """Otherwise "governs: false" is a dead end for the consumer."""
        for row in self._rows(capsys):
            if not row["governs"] and row["verdict"] != "too_early":
                assert row["heldBy"], row

    def test_effective_matches_the_governing_meter(self, capsys):
        """The whole point: one number per provider that respects every meter."""
        rows = self._rows(capsys)
        for provider in {r["provider"] for r in rows}:
            group = [r for r in rows if r["provider"] == provider]
            unscoped = [r for r in group if r["scope"] == "all models"]
            if not unscoped:
                continue
            candidates = [
                r["rateAdjustment"]
                for r in unscoped
                if r["verdict"] != "too_early"
            ]
            if any(r["verdict"] == "too_early" for r in group):
                candidates.append(1.0)
            tightest = min(candidates)
            for row in unscoped:
                assert row["effectiveRateAdjustment"] == pytest.approx(
                    tightest, abs=1e-3
                )

    def test_no_single_combined_instruction(self, capsys):
        """One multiplier for everything reads as advice about everything."""
        main(["--demo", "--json"])
        payload = json.loads(capsys.readouterr().out)
        assert "directive" not in payload


class TestExitCodes:
    def test_success_is_zero(self, capsys):
        assert main(["--demo", "--check"]) == 0
        capsys.readouterr()

    def test_failure_is_non_zero(self, monkeypatch, capsys):
        async def broken(max_age=0):
            return [ProviderSnapshot(key="claude", display_name="Claude", error="nope")]

        monkeypatch.setattr("agents_fuel_gauge.cli.fetch_all", broken)
        assert main(["--check"]) == 1
        capsys.readouterr()

    def test_an_optional_absent_provider_does_not_fail_a_good_reading(
        self, monkeypatch, capsys
    ):
        async def one_provider(max_age=0):
            absent = ProviderSnapshot(
                key="claude", display_name="Claude", installed=False,
                error="Claude CLI is not installed or not on PATH",
            )
            return [absent, demo_snapshots()[1]]

        monkeypatch.setattr("agents_fuel_gauge.cli.fetch_all", one_provider)
        assert main(["--check"]) == 0
        assert "NOT_INSTALLED" in capsys.readouterr().out

    def test_no_installed_providers_is_non_zero(self, monkeypatch, capsys):
        async def no_providers(max_age=0):
            return [
                ProviderSnapshot(
                    key=key, display_name=name, installed=False,
                    error=f"{name} CLI is not installed or not on PATH",
                )
                for key, name in (("claude", "Claude"), ("codex", "Codex"))
            ]

        monkeypatch.setattr("agents_fuel_gauge.cli.fetch_all", no_providers)
        assert main(["--json"]) == 1
        payload = json.loads(capsys.readouterr().out)
        assert [p["installed"] for p in payload["providers"]] == [False, False]


class TestDemoMode:
    def test_demo_never_touches_the_network(self, monkeypatch, capsys):
        def explode(*args, **kwargs):
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
