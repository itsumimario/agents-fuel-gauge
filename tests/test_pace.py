"""Tests for the pace projection.

The headline claim is that the same percentage means opposite things depending
on how much of the window is left, so that is the first thing pinned here.
"""

from datetime import datetime, timedelta, timezone

import pytest

from agents_fuel_gauge.models import (
    MAX_RATE_ADJUSTMENT,
    Gauge,
    ProviderSnapshot,
    directives,
)

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
WEEK = 7 * 86_400
FIVE_HOURS = 5 * 3_600


def gauge(percent: float, elapsed_fraction: float, window: int = WEEK, **kw) -> Gauge:
    """A gauge `elapsed_fraction` of the way through its window."""
    remaining = int(window * (1 - elapsed_fraction))
    return Gauge(
        "7d", kw.pop("scope", "all models"), percent,
        NOW + timedelta(seconds=remaining), window_seconds=window, **kw,
    )


class TestSamePercentOppositeAdvice:
    """The reason this feature exists."""

    def test_91_percent_late_in_the_window_is_fine(self):
        pace = gauge(91, 0.90).pace(NOW)
        assert pace.verdict == "on_track"

    def test_91_percent_early_in_the_window_is_a_crisis(self):
        pace = gauge(91, 0.40).pace(NOW)
        assert pace.verdict == "slow_down"
        assert pace.rate_adjustment < 0.2  # throttle hard

    def test_the_two_differ_only_in_time_remaining(self):
        late, early = gauge(91, 0.90).pace(NOW), gauge(91, 0.40).pace(NOW)
        assert late.verdict != early.verdict
        assert early.ratio > late.ratio


class TestRatio:
    def test_linear_burn_is_exactly_one(self):
        assert gauge(50, 0.50).pace(NOW).ratio == pytest.approx(1.0)

    def test_ratio_is_the_projected_finishing_percentage(self):
        pace = gauge(60, 0.30).pace(NOW)
        assert pace.ratio == pytest.approx(2.0)
        assert pace.to_dict()["projectedUsagePercent"] == pytest.approx(200.0)

    def test_under_budget_projects_below_one(self):
        assert gauge(20, 0.80).pace(NOW).ratio < 1.0


class TestRateAdjustment:
    def test_on_pace_needs_no_change(self):
        assert gauge(50, 0.50).pace(NOW).rate_adjustment == pytest.approx(1.0)

    def test_multiplier_lands_exactly_on_empty(self):
        """Applying it for the rest of the window must reach 100%, not over."""
        g = gauge(80, 0.50)
        pace = g.pace(NOW)
        elapsed = WEEK * 0.5
        remaining = WEEK - elapsed
        current_rate = 0.80 / elapsed
        projected = 0.80 + current_rate * pace.rate_adjustment * remaining
        assert projected == pytest.approx(1.0)

    def test_is_capped_so_it_never_returns_nonsense(self):
        assert gauge(0.01, 0.90).pace(NOW).rate_adjustment <= MAX_RATE_ADJUSTMENT

    def test_exhausted_means_stop(self):
        pace = gauge(100, 0.50).pace(NOW)
        assert pace.verdict == "exhausted"
        assert pace.rate_adjustment == 0.0


class TestArrowDirection:
    """The arrow is read as an instruction, so it has to be one.

    It used to point the way the *number* was drifting — up meant "you are
    running hot" — which is exactly backwards from how anyone reads a glyph.
    """

    def test_burning_too_fast_points_down(self):
        pace = gauge(91, 0.40).pace(NOW)
        assert pace.arrow == "↓"
        assert pace.direction == "down"

    def test_headroom_points_up(self):
        pace = gauge(20, 0.80).pace(NOW)
        assert pace.arrow == "↑"
        assert pace.direction == "up"

    def test_on_budget_points_nowhere(self):
        assert gauge(50, 0.50).pace(NOW).direction == "hold"

    def test_exhausted_points_down_because_there_is_nothing_left(self):
        assert gauge(100, 0.50).pace(NOW).direction == "down"


class TestChangeMagnitude:
    """"Slow down" without "by how much" is half an instruction."""

    def test_slow_down_is_the_complement_of_the_multiplier(self):
        """Throttling to 25% of the rate is slowing down by 75%."""
        pace = gauge(80, 0.50).pace(NOW)
        assert pace.change_percent == pytest.approx(
            (1 - pace.rate_adjustment) * 100
        )
        assert pace.change_label == f"by {pace.change_percent:.0f}%"

    def test_speed_up_is_the_excess_over_the_multiplier(self):
        """Room for 1.6x the rate is speeding up by 60%."""
        pace = gauge(20, 0.80).pace(NOW)
        assert pace.change_percent == pytest.approx(
            (pace.rate_adjustment - 1) * 100
        )

    def test_no_magnitude_where_none_is_meaningful(self):
        for g in (gauge(50, 0.50), gauge(12, 0.02), gauge(100, 0.50)):
            assert g.pace(NOW).change_percent is None

    def test_on_budget_reads_as_words_not_a_useless_zero(self):
        assert gauge(50, 0.50).pace(NOW).change_label == "on budget"

    def test_a_capped_multiplier_is_marked_as_a_floor(self):
        """"by 900%" on a barely-used meter would read as a precise figure."""
        pace = gauge(0.01, 0.90).pace(NOW)
        assert pace.at_cap
        assert pace.change_label.endswith("%+")

    def test_advice_carries_both_readings(self):
        """The change is what you act on; the multiplier is what code applies."""
        # Half the window gone with 80% spent: throttle to a quarter of the
        # rate, which is the same instruction as "slow down by 75%".
        advice = gauge(80, 0.50).pace(NOW).advice
        assert "by 75%" in advice
        assert "25% of its average rate" in advice


class TestEarlyWindowGuard:
    """Minutes into a window, any usage divides into a meaningless ratio."""

    def test_barely_started_window_refuses_to_judge(self):
        pace = gauge(12, 0.02).pace(NOW)
        assert pace.verdict == "too_early"

    def test_early_verdict_advises_no_change(self):
        """1.0 is a safe no-op for anything multiplying this into a rate."""
        assert gauge(12, 0.02).pace(NOW).rate_adjustment == 1.0

    def test_early_verdict_is_not_actionable(self):
        assert gauge(12, 0.02).pace(NOW).actionable is False

    def test_just_past_the_threshold_does_judge(self):
        assert gauge(50, 0.10).pace(NOW).verdict != "too_early"


class TestMissingInformation:
    def test_no_window_length_means_no_projection(self):
        g = Gauge("7d", "all models", 50.0, NOW + timedelta(days=3))
        assert g.pace(NOW) is None

    def test_no_reset_time_means_no_projection(self):
        g = Gauge("7d", "all models", 50.0, None, window_seconds=WEEK)
        assert g.pace(NOW) is None

    def test_window_that_just_opened_means_no_projection(self):
        assert gauge(0, 0.0).pace(NOW) is None


class TestPerMeterDirectives:
    """One entry per meter, never one combined instruction.

    Ranking meters against each other implied a prediction the data cannot
    make: `used / elapsed` is an average over the whole window, so a meter
    burned hard days ago and idle since is indistinguishable from one burning
    now — and would be crowned "runs out first" on usage that had stopped.
    """

    def _snapshot(self, *gauges) -> ProviderSnapshot:
        return ProviderSnapshot(key="claude", display_name="Claude", gauges=list(gauges))

    def test_one_entry_per_meter(self):
        rows = directives([self._snapshot(gauge(95, 0.95), gauge(60, 0.30))], NOW)
        assert len(rows) == 2

    def test_every_entry_names_its_own_meter(self):
        rows = directives([self._snapshot(gauge(60, 0.30, scope="burning fast"))], NOW)
        assert rows[0]["provider"] == "claude"
        assert rows[0]["label"] == "7d burning fast"

    def test_advice_is_scoped_to_that_meter_alone(self):
        """Wording must not read as guidance for everything you run."""
        rows = directives([self._snapshot(gauge(60, 0.30))], NOW)
        assert "this meter" in rows[0]["advice"]

    def test_advice_says_average_not_current(self):
        """One snapshot yields an average rate; claiming "current" overstates it."""
        rows = directives([self._snapshot(gauge(60, 0.30))], NOW)
        assert "average" in rows[0]["advice"]
        assert "current rate" not in rows[0]["advice"]

    def test_each_entry_carries_its_own_reset_time(self):
        rows = directives([self._snapshot(gauge(60, 0.30))], NOW)
        assert rows[0]["resetsAt"] is not None
        assert rows[0]["secondsRemaining"] > 0

    def test_too_early_meters_are_marked_unactionable_not_dropped(self):
        rows = directives([self._snapshot(gauge(12, 0.01))], NOW)
        assert rows[0]["verdict"] == "too_early"
        assert rows[0]["actionable"] is False

    def test_meters_without_enough_data_produce_no_entry(self):
        blind = Gauge("7d", "all models", 50.0, None)
        assert directives([self._snapshot(blind)], NOW) == []
