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
    governing_indexes,
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

    def test_on_pace_points_nowhere(self):
        assert gauge(50, 0.50).pace(NOW).direction == "hold"

    def test_exhausted_points_down_because_there_is_nothing_left(self):
        assert gauge(100, 0.50).pace(NOW).direction == "down"


class TestNoLookalikeGlyphs:
    """`·` and `◦` were a pixel apart and meant opposite things.

    One said the reading is fine, the other said the reading cannot be trusted
    yet. Telling those apart mattered, and at a normal terminal font size it
    was not possible. Only verdicts with a direction keep a glyph.
    """

    def test_states_without_a_direction_use_words(self):
        assert gauge(50, 0.50).pace(NOW).display == "on pace"
        assert gauge(12, 0.02).pace(NOW).display == "too new"

    def test_those_states_carry_no_glyph_at_all(self):
        for g in (gauge(50, 0.50), gauge(12, 0.02)):
            assert g.pace(NOW).arrow == ""

    def test_directions_keep_their_arrow_and_gain_the_size(self):
        assert gauge(91, 0.40).pace(NOW).display.startswith("↓ by ")
        assert gauge(20, 0.80).pace(NOW).display.startswith("↑ by ")

    def test_exhausted_keeps_its_mark_and_says_so(self):
        """The one state where being overlooked costs you something."""
        assert gauge(100, 0.50).pace(NOW).display == "✗ spent"


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

    def test_on_pace_reads_as_words_not_a_useless_zero(self):
        assert gauge(50, 0.50).pace(NOW).change_label == "on pace"

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


class TestMetersAreNotIndependent:
    """The reported bug: one meter's slack licensing another meter's overspend.

    A 5-hour window with room to spare and a weekly window nearly empty is the
    normal state of affairs late in a week, and per-meter advice read it as
    "speed up by 150%" — advice that, followed, blows the weekly cap. Every
    request spends from both, so the tightest meter is the only one with
    anything to say.
    """

    def _snapshot(self, *gauges) -> ProviderSnapshot:
        return ProviderSnapshot(key="claude", display_name="Claude", gauges=list(gauges))

    def test_a_spent_week_silences_an_idle_five_hour_window(self):
        five_hour = gauge(10, 0.80, window=FIVE_HOURS)   # loads of room
        week = gauge(96, 0.50)                            # badly overspent
        snap = self._snapshot(five_hour, week)

        assert five_hour.pace(NOW).verdict == "spare_capacity"
        assert week.pace(NOW).verdict == "slow_down"
        # Only the week governs; the 5h row must not offer its headroom.
        assert governing_indexes(snap, NOW) == {1}

    def test_a_spent_five_hour_window_silences_a_healthy_week(self):
        """Symmetric: a burst limit binds just as hard as a weekly one."""
        five_hour = gauge(95, 0.50, window=FIVE_HOURS)
        week = gauge(20, 0.50)
        snap = self._snapshot(five_hour, week)
        assert governing_indexes(snap, NOW) == {0}

    def test_the_tightest_wins_even_when_both_say_slow_down(self):
        """Acting on the looser of two restrictions still overruns the tighter."""
        mild = gauge(60, 0.40, window=FIVE_HOURS)
        severe = gauge(96, 0.40)
        assert governing_indexes(self._snapshot(mild, severe), NOW) == {1}

    def test_multipliers_are_what_makes_the_comparison_legal(self):
        """Percentages are of different budgets; multipliers are of one rate.

        1% of a five-hour allowance is not 1% of a week's, so the percentages
        cannot be ranked against each other. `rate_adjustment` can: it scales
        request throughput, which is shared.
        """
        five_hour = gauge(10, 0.80, window=FIVE_HOURS)
        week = gauge(96, 0.50)
        # The 5h meter shows the *smaller* percentage and yet is the looser
        # constraint — ranking on percent would pick the wrong one.
        assert five_hour.percent < week.percent
        assert five_hour.pace(NOW).rate_adjustment > week.pace(NOW).rate_adjustment


class TestScopeLimitsWhatAMeterCanConstrain:
    """A per-model cap governs that model, not everything you run."""

    def _snapshot(self, *gauges) -> ProviderSnapshot:
        return ProviderSnapshot(key="claude", display_name="Claude", gauges=list(gauges))

    def test_a_scoped_cap_does_not_throttle_unscoped_work(self):
        """Fable at 96% says nothing about whether you can keep using Sonnet."""
        general = gauge(20, 0.50)
        fable = gauge(96, 0.50, scope="Fable")
        governors = governing_indexes(self._snapshot(general, fable), NOW)
        # Both speak: the general pool is governed by the unscoped meter, and
        # Fable adds a tighter constraint on Fable alone.
        assert governors == {0, 1}

    def test_a_scoped_cap_stays_quiet_when_the_general_pool_is_tighter(self):
        """Its own slack is not spendable, so it has nothing to add."""
        general = gauge(96, 0.50)
        fable = gauge(20, 0.50, scope="Fable")
        assert governing_indexes(self._snapshot(general, fable), NOW) == {0}

    def test_exactly_one_unscoped_meter_ever_governs(self):
        snap = self._snapshot(
            gauge(10, 0.80, window=FIVE_HOURS), gauge(60, 0.50), gauge(30, 0.50)
        )
        governors = governing_indexes(snap, NOW)
        unscoped = [i for i in governors if not snap.gauges[i].scoped]
        assert len(unscoped) == 1


class TestGovernanceEdges:
    def _snapshot(self, *gauges) -> ProviderSnapshot:
        return ProviderSnapshot(key="claude", display_name="Claude", gauges=list(gauges))

    def test_a_window_too_new_to_judge_cannot_govern(self):
        too_new = gauge(2, 0.01, window=FIVE_HOURS)
        week = gauge(60, 0.50)
        assert governing_indexes(self._snapshot(too_new, week), NOW) == {1}

    def test_when_nothing_can_be_judged_every_meter_speaks(self):
        """Silence with no explanation is worse than an unranked reading."""
        snap = self._snapshot(gauge(2, 0.01, window=FIVE_HOURS), gauge(1, 0.01))
        assert governing_indexes(snap, NOW) == {0, 1}

    def test_meters_with_no_reading_are_absent_entirely(self):
        blind = Gauge("7d", "all models", 50.0, None)
        snap = self._snapshot(blind, gauge(60, 0.50))
        assert governing_indexes(snap, NOW) == {1}

    def test_an_exhausted_meter_governs_because_nothing_is_looser_than_zero(self):
        spent = gauge(100, 0.50, window=FIVE_HOURS)
        week = gauge(20, 0.50)
        assert governing_indexes(self._snapshot(spent, week), NOW) == {0}

    def test_no_gauges_at_all_is_not_an_error(self):
        assert governing_indexes(self._snapshot(), NOW) == set()


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
