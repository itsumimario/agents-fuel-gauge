"""Best-effort usage history for plots and recent-rate readouts.

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
from datetime import datetime, timezone
from pathlib import Path
from typing import TypedDict

from . import cache
from .models import ProviderSnapshot

DEDUPE_SECONDS = 5 * 60
RETENTION_SECONDS = 14 * 86_400
TRAILING_SECONDS = 6 * 3_600
MIN_RATE_SPAN_SECONDS = 30 * 60

# A time bucket makes maintenance deterministic across short-lived processes.
# A process-local counter would rarely reach its threshold for `afg --check`,
# while pruning on every append would turn an inexpensive journal into a full
# rewrite once a minute.
PRUNE_INTERVAL_SECONDS = 6 * 3_600


class Sample(TypedDict):
    t: float
    pct: float


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


def trailing_horizon(window_seconds: int | float) -> float:
    """The shorter of six hours and one quarter of this quota window."""
    return min(float(TRAILING_SECONDS), float(window_seconds) / 4.0)


def trailing_rate(
    samples: list[Sample], window_seconds: int | float | None
) -> float | None:
    """Least-squares recent slope in percentage points per day.

    Epoch timestamps are centred before fitting. Besides avoiding needless
    floating-point loss, that makes the arithmetic describe the only quantity
    of interest here: movement within the recent interval, not where Unix time
    happened to put it.
    """
    if not window_seconds or window_seconds <= 0:
        return None
    points = sorted(
        (sample for value in samples if (sample := _sample(value)) is not None),
        key=lambda sample: sample["t"],
    )
    if len(points) < 2:
        return None

    cutoff = points[-1]["t"] - trailing_horizon(window_seconds)
    points = [sample for sample in points if sample["t"] >= cutoff]
    if len(points) < 2 or points[-1]["t"] - points[0]["t"] < MIN_RATE_SPAN_SECONDS:
        return None

    origin = points[0]["t"]
    times = [sample["t"] - origin for sample in points]
    percents = [sample["pct"] for sample in points]
    mean_t = sum(times) / len(times)
    mean_pct = sum(percents) / len(percents)
    denominator = sum((value - mean_t) ** 2 for value in times)
    if denominator == 0:
        return None
    per_second = sum(
        (timestamp - mean_t) * (percent - mean_pct)
        for timestamp, percent in zip(times, percents)
    ) / denominator
    return per_second * 86_400
