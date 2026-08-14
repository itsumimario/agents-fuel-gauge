"""On-disk response cache and 429 backoff.

The vendor usage endpoints are rate limited far below what a dashboard wants.
A dashboard plus a status bar plus a couple of manual `afg --check` runs can
earn a 429, and the limit is on *their* side, so polling faster never helps.

Three mechanisms, all shared across processes via files:

* **Cache.** Every fetch stores the raw response. Any fetch within `max_age`
  reuses it instead of making a request, so frequent callers cost one request
  every five minutes rather than one request each.
* **Single-flight.** A provider lock and post-lock cache check ensure processes
  crossing an expiry boundary together issue one upstream request between them.
* **Backoff.** A 429 records its local cause and an adaptive retry deadline.
  Until then automatic polling does not knock on the closed door again.

Raw payloads are cached rather than parsed snapshots so that a code change to
the parser takes effect immediately instead of being masked by stale objects.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from contextlib import suppress
from pathlib import Path
from typing import TextIO

import fcntl

# Default freshness and dashboard polling cadence. Quota numbers move slowly;
# a five-minute reading remains useful while staying clear of the provider's
# rolling request limits under ordinary use.
DEFAULT_POLL_INTERVAL = 300.0
DEFAULT_MAX_AGE = DEFAULT_POLL_INTERVAL

# Applied when a 429 arrives without a Retry-After header.
DEFAULT_BACKOFF = 120.0
MAX_BACKOFF = 3_600.0
BACKOFF_DECAY = 3_600.0
MAX_BACKOFF_LEVEL = 6


def cache_dir() -> Path:
    base = os.environ.get("AFG_CACHE_DIR") or os.environ.get("XDG_CACHE_HOME")
    root = Path(base).expanduser() if base else Path.home() / ".cache"
    if os.environ.get("AFG_CACHE_DIR"):
        return Path(os.environ["AFG_CACHE_DIR"]).expanduser()
    return root / "agents-fuel-gauge"


def _path(provider: str) -> Path:
    return cache_dir() / f"{provider}.json"


def _lock_path(provider: str) -> Path:
    return cache_dir() / f"{provider}.lock"


def acquire_request_lock(provider: str) -> TextIO | None:
    """Block until this process owns the provider's upstream request slot."""
    directory = cache_dir()
    handle = None
    try:
        directory.mkdir(parents=True, exist_ok=True)
        handle = _lock_path(provider).open("a+", encoding="utf-8")
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        return handle
    except OSError:
        if handle is not None:
            handle.close()
        return None


def release_request_lock(handle: TextIO | None) -> None:
    """Release a request slot; closing also releases it after exceptions."""
    if handle is None:
        return
    with suppress(OSError):
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    handle.close()


def _read(provider: str) -> dict:
    try:
        return json.loads(_path(provider).read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _write(provider: str, record: dict) -> None:
    """Atomic replace: a half-written cache file must never be readable.

    Several `afg` processes can be running at once — a dashboard, a status bar,
    a script — and a torn read would look like corruption to all of them.
    """
    directory = cache_dir()
    try:
        directory.mkdir(parents=True, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(
            "w", dir=directory, delete=False, encoding="utf-8"
        )
        with handle as tmp:
            json.dump(record, tmp)
        os.replace(handle.name, _path(provider))
    except OSError:
        pass  # a cache that cannot be written is a slowdown, not a failure


def load(provider: str, max_age: float = DEFAULT_MAX_AGE) -> tuple[dict | None, float]:
    """Return (payload, age_seconds) if fresh enough, else (None, age)."""
    record = _read(provider)
    payload = record.get("payload")
    fetched_at = record.get("fetched_at")
    if payload is None or not isinstance(fetched_at, (int, float)):
        return None, float("inf")
    age = max(0.0, time.time() - fetched_at)
    return (payload, age) if age <= max_age else (None, age)


def load_stale(provider: str) -> tuple[dict | None, float]:
    """The last payload regardless of age — the fallback when a fetch fails."""
    record = _read(provider)
    payload = record.get("payload")
    fetched_at = record.get("fetched_at")
    if payload is None or not isinstance(fetched_at, (int, float)):
        return None, float("inf")
    return payload, max(0.0, time.time() - fetched_at)


def load_fetched_since(
    provider: str, earliest: float
) -> tuple[dict | None, float]:
    """Return a response another caller fetched after this attempt began."""
    record = _read(provider)
    payload = record.get("payload")
    fetched_at = record.get("fetched_at")
    if (
        payload is None
        or not isinstance(fetched_at, (int, float))
        or fetched_at < earliest
    ):
        return None, float("inf")
    return payload, max(0.0, time.time() - fetched_at)


def store(provider: str, payload: dict) -> None:
    record = _read(provider)
    record.update({"payload": payload, "fetched_at": time.time()})
    record.pop("retry_after", None)  # a success clears any standing backoff
    _write(provider, record)


def blocked_for(provider: str) -> float:
    """Seconds left on a 429 backoff, or 0 when it is fine to try again."""
    retry_after = _read(provider).get("retry_after")
    if not isinstance(retry_after, (int, float)):
        return 0.0
    return max(0.0, retry_after - time.time())


def rate_limit_status(provider: str) -> dict | None:
    """Describe the most recent 429, retained even after a later success."""
    status = _read(provider).get("last_rate_limit")
    return status if isinstance(status, dict) else None


def block(provider: str, seconds: float | None, *, forced: bool = False) -> None:
    """Record a 429 with incident-aware exponential backoff metadata."""
    record = _read(provider)
    now = time.time()
    previous = record.get("last_rate_limit")
    previous_at = previous.get("at") if isinstance(previous, dict) else None
    previous_level = previous.get("level") if isinstance(previous, dict) else None
    repeating = (
        isinstance(previous_at, (int, float))
        and isinstance(previous_level, int)
        and now - previous_at <= BACKOFF_DECAY
    )
    level = min(previous_level + 1, MAX_BACKOFF_LEVEL) if repeating else 1
    adaptive = min(DEFAULT_BACKOFF * (2 ** (level - 1)), MAX_BACKOFF)
    server = seconds if isinstance(seconds, (int, float)) and seconds > 0 else 0.0
    duration = max(adaptive, server)
    record["retry_after"] = now + duration
    record["last_rate_limit"] = {
        "at": now,
        "seconds": duration,
        "pid": os.getpid(),
        "mode": "manual" if forced else "automatic",
        "source": "server" if server >= adaptive else "adaptive",
        "level": level,
    }
    _write(provider, record)


def clear() -> None:
    for path in cache_dir().glob("*.json"):
        try:
            path.unlink()
        except OSError:
            pass
