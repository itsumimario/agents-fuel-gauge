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
