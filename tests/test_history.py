"""History stays useful even when polling overlaps or one line is damaged."""

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


def test_recent_rate_sees_rationing_after_an_early_binge():
    """The recent readout exists to make changed behaviour visible.

    Eighty percent in two days projects a ruinous whole-window average. Once
    the user settles at four percentage points per day, the trailing fit should
    say four rather than continuing to punish the old binge.
    """
    samples = [
        {"t": 0.0, "pct": 0.0},
        {"t": 2 * DAY, "pct": 80.0},
    ]
    for hours in (2, 4, 6):
        samples.append(
            {"t": 2 * DAY + hours * 3_600, "pct": 80 + 4 * hours / 24}
        )

    recent = history.trailing_rate(samples, WEEK)
    whole_window_average = samples[-1]["pct"] / (samples[-1]["t"] / DAY)

    assert recent == pytest.approx(4.0)
    assert whole_window_average > 30


def test_recent_rate_requires_two_points_spanning_half_an_hour():
    assert history.trailing_rate([{"t": 0, "pct": 5}], WEEK) is None
    assert history.trailing_rate(
        [{"t": 0, "pct": 5}, {"t": 29 * 60, "pct": 6}], WEEK
    ) is None
    assert history.trailing_rate(
        [{"t": 0, "pct": 5}, {"t": 30 * 60, "pct": 6}], WEEK
    ) is not None


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
    monkeypatch.setattr(sources.history, "record_snapshots", calls.append)

    snapshots = await sources.fetch_all(max_age=123)

    assert calls == [snapshots]
    assert [snapshot.key for snapshot in snapshots] == ["claude", "codex"]
