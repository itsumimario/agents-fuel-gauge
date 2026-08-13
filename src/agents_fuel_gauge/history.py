"""Best-effort usage history for plots and rate-regime readouts.

The response cache answers "what is the latest payload?" and is deliberately
overwritten. History answers a different question: whether the user changed
course after that payload arrived. Keeping the two stores separate lets cache
expiry and cache clearing stay cheap without making the trace fragile.

Each series is JSONL rather than one growing JSON document. Several `afg`
processes commonly overlap, and one `O_APPEND` write gives each sample a much
smaller corruption surface than a read-modify-write of the whole trace. Reads
still distrust every line: history is useful evidence, never a reason for a
dashboard to fail.
"""

from __future__ import annotations

import json
import math
import os
import re
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TypedDict

from . import cache
from .models import ProviderSnapshot

DEDUPE_SECONDS = 5 * 60
RETENTION_SECONDS = 14 * 86_400
SEGMENT_RATE_THRESHOLD = 0.35
MAX_SEGMENTS = 3
MAX_EPISODES = 5
EPISODE_BINS = 96
EPISODE_RADIUS = 5
EPISODE_SMOOTH_RADIUS = 3
EPISODE_LINEAR_ERROR_PCT = 0.3
EPISODE_MIN_POINTS = 7
EPISODE_MIN_LINEAR_POINTS = 7
EPISODE_MIN_VARIABLE_POINTS = 3

# A time bucket makes maintenance deterministic across short-lived processes.
# A process-local counter would rarely reach its threshold for `afg --check`,
# while pruning on every append would turn an inexpensive journal into a full
# rewrite once a minute.
PRUNE_INTERVAL_SECONDS = 6 * 3_600


class Sample(TypedDict):
    t: float
    pct: float


@dataclass(frozen=True)
class Corner:
    """One information-bearing rise in an integer-percent staircase."""

    t: float
    pct: float
    delta_pct: float


@dataclass(frozen=True)
class Segment:
    """A contiguous chunk whose percent ticks support one average rate."""

    start: float
    end: float
    delta_pct: float
    rate_per_day: float
    tick_count: int


@dataclass(frozen=True)
class Episode:
    """One contiguous linear or variable-shape portion of a usage trace."""

    start: float
    end: float
    delta_pct: float
    rate_per_day: float
    linear: bool


def history_dir() -> Path:
    """History follows the same XDG and `AFG_CACHE_DIR` rules as the cache."""
    return cache.cache_dir() / "history"


def series_key(provider: str, label: str) -> str:
    """A readable filename stem with separators and traversal removed."""
    raw = f"{provider}-{label}".casefold()
    slug = re.sub(r"[^a-z0-9._-]+", "-", raw).strip("-._") or "series"
    # Provider labels are normally tiny. The cap is defensive: an upstream
    # label should not be able to exceed a filesystem's component limit.
    return slug[:180].rstrip("-._") or "series"


def series_path(provider: str, label: str) -> Path:
    return history_dir() / f"{series_key(provider, label)}.jsonl"


def _sample(value: object) -> Sample | None:
    if not isinstance(value, dict):
        return None
    timestamp = value.get("t")
    percent = value.get("pct")
    if (
        isinstance(timestamp, bool)
        or isinstance(percent, bool)
        or not isinstance(timestamp, (int, float))
        or not isinstance(percent, (int, float))
    ):
        return None
    timestamp = float(timestamp)
    percent = float(percent)
    if not math.isfinite(timestamp) or not math.isfinite(percent):
        return None
    return {"t": timestamp, "pct": percent}


def _read_path(path: Path) -> list[Sample]:
    samples: list[Sample] = []
    try:
        # Decode one line at a time. Opening in text mode would let one invalid
        # UTF-8 byte raise from the iterator itself, before the per-line error
        # boundary had a chance to discard it.
        with path.open("rb") as lines:
            for line in lines:
                try:
                    parsed = _sample(json.loads(line))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                if parsed is not None:
                    samples.append(parsed)
    except OSError:
        return []
    return sorted(samples, key=lambda sample: sample["t"])


def read_series(provider: str, label: str) -> list[Sample]:
    """Return every readable sample; a damaged line does not poison its file."""
    return _read_path(series_path(provider, label))


def _as_timestamp(value: datetime | float | int) -> float:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.timestamp()
    return float(value)


def read_window(
    provider: str,
    label: str,
    resets_at: datetime | float | int | None,
    window_seconds: int | float | None,
) -> list[Sample]:
    """Read the series portion belonging to the gauge's current window."""
    if resets_at is None or not window_seconds or window_seconds <= 0:
        return []
    opened_at = _as_timestamp(resets_at) - float(window_seconds)
    return [
        sample
        for sample in read_series(provider, label)
        if sample["t"] >= opened_at
    ]


def corners(samples: list[Sample]) -> list[Corner]:
    """Place each rising percent tick halfway between its surrounding polls."""
    points = sorted(
        (sample for value in samples if (sample := _sample(value)) is not None),
        key=lambda sample: sample["t"],
    )
    extracted = []
    for previous, current in zip(points, points[1:]):
        delta = current["pct"] - previous["pct"]
        if delta <= 0:
            continue
        extracted.append(
            Corner(
                t=(previous["t"] + current["t"]) / 2,
                pct=(previous["pct"] + current["pct"]) / 2,
                delta_pct=delta,
            )
        )
    return extracted


def _segment(
    start: float,
    end: float,
    delta_pct: float,
    tick_count: int,
) -> Segment:
    return Segment(
        start=start,
        end=end,
        delta_pct=delta_pct,
        rate_per_day=delta_pct / (end - start) * 86_400,
        tick_count=tick_count,
    )


def _merge_segments(left: Segment, right: Segment) -> Segment:
    return _segment(
        left.start,
        right.end,
        left.delta_pct + right.delta_pct,
        left.tick_count + right.tick_count,
    )


def _relative_rate_difference(left: Segment, right: Segment) -> float:
    return abs(left.rate_per_day - right.rate_per_day) / max(
        left.rate_per_day, right.rate_per_day
    )


def segments(
    samples: list[Sample],
    relative_threshold: float = SEGMENT_RATE_THRESHOLD,
    max_segments: int = MAX_SEGMENTS,
) -> list[Segment]:
    """Infer at most three steady-rate chunks from integer-percent ticks.

    A 35% relative boundary absorbs ordinary midpoint jitter while retaining
    the motivating 20-to-10 percent/day correction with comfortable margin.
    """
    changes = corners(samples)
    found = []
    for previous, current in zip(changes, changes[1:]):
        if current.t <= previous.t:
            continue
        delta_pct = current.pct - previous.pct
        if delta_pct <= 0:
            continue
        # Midpoint percent levels split a multi-point jump across both sides
        # of its corner. Otherwise an overnight gap's whole change lands in
        # one short-looking interval and invents a burst followed by a lull.
        tick_count = max(1, int(round(delta_pct)))
        found.append(
            _segment(
                previous.t,
                current.t,
                delta_pct,
                tick_count,
            )
        )

    while len(found) > 1:
        differences = [
            _relative_rate_difference(left, right)
            for left, right in zip(found, found[1:])
        ]
        weak_pairs = [
            index
            for index, (left, right) in enumerate(zip(found, found[1:]))
            if left.tick_count < 2 or right.tick_count < 2
        ]
        candidates = weak_pairs or list(range(len(differences)))
        best = min(candidates, key=lambda index: (differences[index], index))
        if (
            not weak_pairs
            and len(found) <= max_segments
            and differences[best] > relative_threshold
        ):
            break
        found[best : best + 2] = [_merge_segments(found[best], found[best + 1])]

    # Corners only exist where percent moved, so a finished burst would keep
    # its old rate on screen forever: the latest segment stays parked at its
    # last tick while a user who eased off entirely is still told to slow
    # down — the exact misreading this module exists to correct. Silence is
    # evidence too, once there is enough of it to mean something: longer than
    # the segment's own tick spacing and it joins the denominator; shorter is
    # just the ordinary wait between ticks.
    last_seen = max(
        (sample["t"] for value in samples if (sample := _sample(value)) is not None),
        default=None,
    )
    if found and last_seen is not None:
        latest = found[-1]
        expected_gap = (latest.end - latest.start) / latest.tick_count
        if last_seen - latest.end > expected_gap:
            found[-1] = _segment(
                latest.start, last_seen, latest.delta_pct, latest.tick_count
            )

    # One observed tick has no neighbouring evidence to absorb it, so it is
    # safer to report no regime than to promote a quantization accident.
    return found if not found or found[0].tick_count >= 2 else []


def _history_points(samples: list[Sample]) -> list[Sample]:
    """Validate, sort, and collapse concurrent readings at one timestamp."""
    by_timestamp: dict[float, Sample] = {}
    for value in samples:
        sample = _sample(value)
        if sample is not None:
            by_timestamp[sample["t"]] = sample
    return [by_timestamp[timestamp] for timestamp in sorted(by_timestamp)]


def _percent_at(points: list[Sample], timestamp: float) -> float:
    """Linearly interpolate a percentage between two polling samples."""
    if timestamp <= points[0]["t"]:
        return points[0]["pct"]
    if timestamp >= points[-1]["t"]:
        return points[-1]["pct"]
    low = 0
    high = len(points) - 1
    while high - low > 1:
        middle = (low + high) // 2
        if points[middle]["t"] <= timestamp:
            low = middle
        else:
            high = middle
    left = points[low]
    right = points[high]
    span = right["t"] - left["t"]
    if span <= 0:
        return right["pct"]
    progress = (timestamp - left["t"]) / span
    return left["pct"] + (right["pct"] - left["pct"]) * progress


def _linear_fit(points: list[Sample]) -> tuple[float, float]:
    """Return slope per day and largest residual for a set of points."""
    slope, intercept = _linear_coefficients(points)
    residual = max(
        abs(point["pct"] - (slope * point["t"] + intercept))
        for point in points
    )
    return slope * 86_400, residual


def _linear_coefficients(points: list[Sample]) -> tuple[float, float]:
    """Return the least-squares slope per second and intercept."""
    mean_t = sum(point["t"] for point in points) / len(points)
    mean_pct = sum(point["pct"] for point in points) / len(points)
    denominator = sum((point["t"] - mean_t) ** 2 for point in points)
    slope = (
        sum(
            (point["t"] - mean_t) * (point["pct"] - mean_pct)
            for point in points
        )
        / denominator
        if denominator
        else 0.0
    )
    intercept = mean_pct - slope * mean_t
    return slope, intercept


def _resample(points: list[Sample], bins: int) -> list[Sample]:
    """Put irregular polling on an even time axis for shape classification."""
    start = points[0]["t"]
    step = (points[-1]["t"] - start) / bins
    return [
        {
            "t": start + index * step,
            "pct": _percent_at(points, start + index * step),
        }
        for index in range(bins + 1)
    ]


def _smooth_linear_noise(points: list[Sample], radius: int) -> list[Sample]:
    """Suppress integer-meter steps without rounding away real curvature.

    A local least-squares estimate preserves an actual straight line exactly.
    That matters at both ends of the trace, where an ordinary moving average
    becomes one-sided and falsely bends a steep but steady rate.
    """
    smoothed = []
    for index, point in enumerate(points):
        window = points[
            max(0, index - radius) : min(len(points), index + radius + 1)
        ]
        slope, intercept = _linear_coefficients(window)
        smoothed.append(
            {
                "t": point["t"],
                "pct": slope * point["t"] + intercept,
            }
        )
    return smoothed


def _runs(flags: list[bool]) -> list[tuple[bool, int, int]]:
    """Run-length encode a linear/nonlinear classification."""
    found: list[tuple[bool, int, int]] = []
    for index, flag in enumerate(flags):
        if not found or found[-1][0] != flag:
            found.append((flag, index, index))
        else:
            previous = found[-1]
            found[-1] = (previous[0], previous[1], index)
    return found


def _absorb_short_runs(flags: list[bool]) -> list[bool]:
    """Keep brief quantization artifacts from becoming visual episodes.

    A straight portion needs enough consecutive evidence to act as a useful
    delimiter. Variable portions can be shorter because a real transition
    between two sustained slopes is often compact.
    """
    cleaned = list(flags)
    while True:
        found = _runs(cleaned)
        speck = next(
            (
                (index, run)
                for index, run in enumerate(found)
                if len(found) > 1
                and run[2] - run[1] + 1
                < (
                    EPISODE_MIN_LINEAR_POINTS
                    if run[0]
                    else EPISODE_MIN_VARIABLE_POINTS
                )
            ),
            None,
        )
        if speck is None:
            return cleaned
        index, (_, start, end) = speck
        replacement = found[index - 1][0] if index else found[index + 1][0]
        cleaned[start : end + 1] = [replacement] * (end - start + 1)


def episodes(
    samples: list[Sample],
    max_episodes: int = MAX_EPISODES,
) -> list[Episode]:
    """Slice history at transitions between linear and variable behavior.

    Provider percentages are integer staircases and polling is irregular. AFG
    first resamples and smooths the recorded span, then classifies each local
    window by the error of its best straight-line fit. Sustained straight
    portions act as delimiters; everything between them stays together as one
    variable-shape portion. This keeps a roller-coaster interval intact instead
    of reducing it to several unrelated fitted rates.
    """
    points = _history_points(samples)
    if (
        len(points) < EPISODE_MIN_POINTS
        or points[-1]["t"] <= points[0]["t"]
        or max_episodes <= 0
    ):
        return []

    bins = min(EPISODE_BINS, len(points) - 1)
    resampled = _resample(points, bins)
    smoothed = _smooth_linear_noise(
        resampled, min(EPISODE_SMOOTH_RADIUS, max(1, bins // 4))
    )
    radius = min(EPISODE_RADIUS, max(2, bins // 4))
    window_size = min(len(smoothed), radius * 2 + 1)
    flags = []
    for index in range(len(smoothed)):
        start = max(0, min(index - radius, len(smoothed) - window_size))
        window = smoothed[start : start + window_size]
        _, residual = _linear_fit(window)
        flags.append(residual <= EPISODE_LINEAR_ERROR_PCT)
    flags = _absorb_short_runs(flags)

    classified = _runs(flags)
    boundaries = [resampled[0]["t"]]
    for left, right in zip(classified, classified[1:]):
        boundaries.append(
            (resampled[left[2]]["t"] + resampled[right[1]]["t"]) / 2
        )
    boundaries.append(resampled[-1]["t"])

    found = []
    for index, (linear, start_index, end_index) in enumerate(classified):
        start = boundaries[index]
        end = boundaries[index + 1]
        start_pct = _percent_at(resampled, start)
        end_pct = _percent_at(resampled, end)
        if linear:
            rate_per_day, _ = _linear_fit(
                smoothed[start_index : end_index + 1]
            )
        else:
            rate_per_day = (end_pct - start_pct) / (end - start) * 86_400
        found.append(
            Episode(
                start=start,
                end=end,
                delta_pct=end_pct - start_pct,
                rate_per_day=rate_per_day,
                linear=linear,
            )
        )
    return found[-max_episodes:]


def _rewrite(path: Path, samples: list[Sample]) -> None:
    """Atomic replacement keeps pruning invisible to concurrent readers."""
    try:
        handle = tempfile.NamedTemporaryFile(
            "w", dir=path.parent, delete=False, encoding="utf-8"
        )
        with handle as tmp:
            for sample in samples:
                tmp.write(json.dumps(sample, separators=(",", ":")) + "\n")
        os.replace(handle.name, path)
    except OSError:
        pass  # history is a display enhancement, never a reason polling fails


def _prune(path: Path, now: float) -> None:
    cutoff = now - RETENTION_SECONDS
    _rewrite(path, [sample for sample in _read_path(path) if sample["t"] >= cutoff])


def append_sample(
    provider: str,
    label: str,
    percent: float,
    at: datetime | float | int | None = None,
) -> None:
    """Append one point unless it is a very recent repeat of the same value."""
    timestamp = time.time() if at is None else _as_timestamp(at)
    path = series_path(provider, label)
    existing = _read_path(path)
    newest = existing[-1] if existing else None
    age = timestamp - newest["t"] if newest is not None else None
    if (
        newest is not None
        and newest["pct"] == float(percent)
        and age is not None
        and 0 <= age < DEDUPE_SECONDS
    ):
        return

    sample: Sample = {"t": timestamp, "pct": float(percent)}
    line = (json.dumps(sample, separators=(",", ":")) + "\n").encode()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(descriptor, line)
        finally:
            os.close(descriptor)
    except OSError:
        return  # a missing trace is less useful, not a failed quota poll

    if (
        newest is not None
        and int(newest["t"] // PRUNE_INTERVAL_SECONDS)
        != int(timestamp // PRUNE_INTERVAL_SECONDS)
    ):
        _prune(path, timestamp)


def record_snapshots(snapshots: list[ProviderSnapshot]) -> None:
    """Record only live successes, never cached or carried-forward readings."""
    for snapshot in snapshots:
        if not snapshot.ok or snapshot.stale:
            continue
        captured_at = snapshot.captured_at or datetime.now(timezone.utc)
        for gauge in snapshot.gauges:
            append_sample(snapshot.key, gauge.label, gauge.percent, captured_at)
