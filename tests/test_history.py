"""History stays useful even when polling overlaps or one line is damaged."""

import math
from datetime import datetime, timedelta, timezone

import pytest

from agents_fuel_gauge import history, sources
from agents_fuel_gauge.models import Gauge, ProviderSnapshot

DAY = 86_400
WEEK = 7 * DAY
NOW = datetime(2026, 8, 12, 12, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def isolated_history(tmp_path, monkeypatch):
    """No test may read or alter the user's real quota trace."""
    monkeypatch.setenv("AFG_CACHE_DIR", str(tmp_path))


def _quantized_samples(regimes, sample_seconds=10 * 60):
    """Sample an ideal piecewise rate through the integer meters providers expose."""
    boundaries = []
    elapsed = 0.0
    for days, rate in regimes:
        elapsed += days * DAY
        boundaries.append((elapsed, rate))

    samples = []
    for timestamp in range(0, int(elapsed) + 1, sample_seconds):
        used = 0.0
        opened = 0.0
        for boundary, rate in boundaries:
            used += max(0.0, min(timestamp, boundary) - opened) * rate / DAY
            opened = boundary
        samples.append({"t": float(timestamp), "pct": float(int(used))})
    return samples


def _shaped_samples(sample_seconds=30 * 60):
    """Alternate steady slopes with monotonic but visibly curved usage."""
    samples = []
    used = 0.0
    previous_rate = 10.0
    for timestamp in range(0, 3 * DAY + 1, sample_seconds):
        day = timestamp / DAY
        if day < 1:
            rate = 10.0
        elif day < 2:
            rate = 18 + 12 * math.sin(4 * math.pi * (day - 1))
        else:
            rate = 6.0
        if timestamp:
            used += (previous_rate + rate) / 2 * sample_seconds / DAY
        samples.append({"t": float(timestamp), "pct": float(int(used))})
        previous_rate = rate
    return samples


def test_append_and_read_round_trip_preserves_the_sample():
    history.append_sample("claude", "7d all models", 42.5, NOW)

    assert history.read_series("claude", "7d all models") == [
        {"t": NOW.timestamp(), "pct": 42.5}
    ]


def test_series_filename_contains_no_provider_supplied_separators():
    path = history.series_path("codex", "7d GPT / strange:model")
    assert path.parent == history.history_dir()
    assert path.name == "codex-7d-gpt-strange-model.jsonl"


def test_same_value_is_deduped_only_while_it_is_recent():
    """Quiet meters should stay compact without erasing a long idle stretch."""
    start = NOW.timestamp()
    history.append_sample("codex", "7d all models", 20, start)
    history.append_sample("codex", "7d all models", 20, start + 299)
    history.append_sample("codex", "7d all models", 21, start + 60)
    history.append_sample("codex", "7d all models", 21, start + 361)

    assert [sample["pct"] for sample in history.read_series(
        "codex", "7d all models"
    )] == [20, 21, 21]


def test_occasional_prune_drops_only_samples_older_than_fourteen_days():
    old = NOW - timedelta(days=15)
    retained = NOW - timedelta(days=13)
    history.append_sample("claude", "7d all models", 10, old)
    history.append_sample("claude", "7d all models", 20, retained)
    history.append_sample("claude", "7d all models", 30, NOW)

    assert history.read_series("claude", "7d all models") == [
        {"t": retained.timestamp(), "pct": 20.0},
        {"t": NOW.timestamp(), "pct": 30.0},
    ]


def test_a_corrupt_line_does_not_hide_the_samples_around_it():
    history.append_sample("claude", "5h all models", 10, NOW)
    path = history.series_path("claude", "5h all models")
    with path.open("a", encoding="utf-8") as lines:
        lines.write("a process stopped halfway through {\n")
        lines.write('{"t":"yesterday","pct":99}\n')
    with path.open("ab") as lines:
        lines.write(b"\xff\xfe not utf-8\n")
    history.append_sample("claude", "5h all models", 12, NOW + timedelta(hours=1))

    assert [sample["pct"] for sample in history.read_series(
        "claude", "5h all models"
    )] == [10, 12]


def test_window_slice_keeps_the_backward_sample_that_marks_a_reset():
    """A falling percentage is evidence of a new window, not bad data."""
    reset = NOW + timedelta(days=6)
    opened = reset - timedelta(seconds=WEEK)
    history.append_sample("codex", "7d all models", 90, opened - timedelta(seconds=1))
    history.append_sample("codex", "7d all models", 5, opened + timedelta(seconds=1))
    history.append_sample("codex", "7d all models", 9, opened + timedelta(days=1))

    samples = history.read_window("codex", "7d all models", reset, WEEK)
    assert [sample["pct"] for sample in samples] == [5, 9]


def test_corners_keep_only_rising_ticks_at_sample_midpoints():
    """Staircase plateaus and provider corrections must not become rates."""
    samples = [
        {"t": 0, "pct": 10},
        {"t": 60, "pct": 10},
        {"t": 120, "pct": 12},
        {"t": 180, "pct": 12},
        {"t": 300, "pct": 11},
        {"t": 360, "pct": 14},
    ]

    extracted = history.corners(samples)

    assert [(corner.t, corner.delta_pct) for corner in extracted] == [
        (90, 2),
        (330, 3),
    ]


def test_segments_recover_a_corrected_binge_from_integer_samples():
    """The history line should preserve a real course correction as context."""
    found = history.segments(_quantized_samples([(2, 20), (4, 10)]))

    assert len(found) == 2
    assert [segment.rate_per_day for segment in found] == pytest.approx(
        [20, 10], rel=0.04
    )
    assert found[0].end == pytest.approx(2 * DAY, abs=3 * 3_600)


def test_slow_integer_meter_stays_one_regime():
    """Twelve-hour tick spacing must not manufacture alternating rates."""
    found = history.segments(_quantized_samples([(7, 2)]))

    assert len(found) == 1
    assert found[0].rate_per_day == pytest.approx(2, rel=0.04)


def test_steady_weekly_rate_stays_one_regime():
    """Quantization should not add a story when the user's pace never changed."""
    found = history.segments(_quantized_samples([(4, 14.3)]))

    assert len(found) == 1
    assert found[0].rate_per_day == pytest.approx(14.3, rel=0.04)


def test_sampling_gap_does_not_split_a_steady_regime():
    """Time without afg running is part of the measured average, not a reset."""
    samples = [
        sample
        for sample in _quantized_samples([(7, 10)])
        if not 2.25 * DAY < sample["t"] < 3.25 * DAY
    ]

    found = history.segments(samples)

    assert len(found) == 1
    assert found[0].rate_per_day == pytest.approx(10, rel=0.04)


def test_informative_silence_joins_the_latest_segment():
    """A user who eased off entirely must not keep reading their old rate.

    Corners exist only where percent moved, so a finished burst would stay
    parked at its last tick and judge yesterday's pace forever — telling an
    idle user to slow down, the exact misreading segmentation exists to fix.
    Silence longer than the segment's own tick spacing is evidence.
    """
    samples = _quantized_samples([(2, 10), (1, 0)])

    found = history.segments(samples)

    assert len(found) == 1
    assert found[-1].end == samples[-1]["t"]
    assert found[-1].rate_per_day < 8  # ~10 if the idle day were ignored


def test_ordinary_between_tick_silence_changes_nothing():
    """The wait for the next integer tick is not a slowdown."""
    samples = _quantized_samples([(2, 10)])

    found = history.segments(samples)

    assert len(found) == 1
    assert found[0].rate_per_day == pytest.approx(10, rel=0.04)


def test_five_distinct_rates_are_capped_at_three_segments():
    """A one-line readout must stay scannable even when usage is erratic."""
    samples = _quantized_samples(
        [(0.5, 4), (0.5, 8), (0.5, 16), (0.5, 32), (0.5, 64)]
    )

    found = history.segments(samples)

    assert 1 < len(found) <= 3


def test_one_tick_fragment_is_absorbed_by_a_neighbor():
    """One quantized step is noise and must never become a reported regime."""
    samples = [
        {"t": 0, "pct": 0},
        {"t": 60, "pct": 1},
        {"t": 120, "pct": 2},
        {"t": 180, "pct": 8},
    ]

    found = history.segments(samples)

    assert len(found) == 1
    assert found[0].tick_count >= 2


def test_episodes_keep_a_variable_shape_between_linear_delimiters():
    found = history.episodes(_shaped_samples())

    assert [portion.linear for portion in found] == [True, False, True]
    assert found[0].end == pytest.approx(DAY, abs=4 * 3_600)
    assert found[1].end == pytest.approx(2 * DAY, abs=4 * 3_600)
    assert [found[0].rate_per_day, found[2].rate_per_day] == pytest.approx(
        [10, 6], abs=0.5
    )


@pytest.mark.parametrize(
    ("days", "rate"),
    [(4, 0), (4, 2.2), (4, 10), (3, 32)],
)
def test_steady_integer_staircase_is_one_linear_episode(days, rate):
    found = history.episodes(_quantized_samples([(days, rate)]))

    assert len(found) == 1
    assert found[0].linear is True
    assert found[0].rate_per_day == pytest.approx(rate, abs=0.25)


def test_details_keep_only_the_five_newest_episodes():
    samples = _quantized_samples(
        [(1.5, 8), (1.5, 24), (1.5, 4), (1.5, 18)],
        sample_seconds=30 * 60,
    )
    all_portions = history.episodes(samples, max_episodes=99)

    assert len(all_portions) > 5
    assert history.episodes(samples) == all_portions[-5:]


def test_recording_ignores_cached_carried_forward_and_failed_snapshots():
    """Repeated old numbers would turn cache hits into a fake flat trace."""
    gauge = Gauge("7d", "all models", 50, window_seconds=WEEK)
    fresh = ProviderSnapshot(
        key="fresh", display_name="Fresh", gauges=[gauge], captured_at=NOW
    )
    stale = ProviderSnapshot(
        key="stale", display_name="Stale", gauges=[gauge], captured_at=NOW,
        stale=True,
    )
    failed = ProviderSnapshot(
        key="failed", display_name="Failed", gauges=[gauge], captured_at=NOW,
        error="network error",
    )

    history.record_snapshots([fresh, stale, failed])

    assert len(history.read_series("fresh", gauge.label)) == 1
    assert history.read_series("stale", gauge.label) == []
    assert history.read_series("failed", gauge.label) == []


async def test_fetch_all_records_at_the_shared_caller_choke_point(monkeypatch):
    """The TUI and both one-shot formats must not grow separate record paths."""

    async def fake_claude(client, max_age):
        return ProviderSnapshot(key="claude", display_name="Claude")

    async def fake_codex(client, max_age):
        return ProviderSnapshot(key="codex", display_name="Codex")

    calls = []
    monkeypatch.setattr(sources, "fetch_claude", fake_claude)
    monkeypatch.setattr(sources, "fetch_codex", fake_codex)
    monkeypatch.setattr(sources, "provider_cli_installed", lambda provider: True)
    monkeypatch.setattr(sources.history, "record_snapshots", calls.append)

    snapshots = await sources.fetch_all(max_age=123)

    assert calls == [snapshots]
    assert [snapshot.key for snapshot in snapshots] == ["claude", "codex"]


async def test_fetch_all_skips_a_provider_whose_cli_is_not_installed(monkeypatch):
    """Absence must not trigger credential reads or network requests."""
    calls = []

    async def fake_claude(client, max_age):
        raise AssertionError("an absent CLI must never reach its fetcher")

    async def fake_codex(client, max_age):
        calls.append(max_age)
        return ProviderSnapshot(key="codex", display_name="Codex")

    monkeypatch.setattr(sources, "fetch_claude", fake_claude)
    monkeypatch.setattr(sources, "fetch_codex", fake_codex)
    monkeypatch.setattr(
        sources,
        "provider_cli_installed",
        lambda provider: provider == "codex",
    )

    claude, codex = await sources.fetch_all(max_age=123)

    assert claude.installed is False
    assert "not installed" in claude.error
    assert codex.installed is True
    assert calls == [123]
