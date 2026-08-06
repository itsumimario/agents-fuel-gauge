"""On-disk response cache and 429 backoff.

The vendor usage endpoints are rate limited far below what a dashboard wants.
A 60-second refresh plus a status bar plus a couple of manual `afg --check`
runs is enough to earn a 429, and the limit is on *their* side, so polling
faster never helps.

Two mechanisms, both shared across processes via files:

* **Cache.** Every fetch stores the raw response. Any fetch within `max_age`
  reuses it instead of making a request, so ten callers a minute cost one
  request rather than ten.
* **Backoff.** A 429 records when the server said to come back, and until then
  no request is attempted at all — retrying into a closed door is what turns
  one 429 into a stream of them.

Raw payloads are cached rather than parsed snapshots so that a code change to
the parser takes effect immediately instead of being masked by stale objects.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path

# Default freshness. Quota numbers move slowly; a minute-old reading is fine
# and costs nothing, whereas a fresh one can cost you the next ten.
DEFAULT_MAX_AGE = 60.0

# Applied when a 429 arrives without a Retry-After header.
DEFAULT_BACKOFF = 120.0


def cache_dir() -> Path:
    base = os.environ.get("AFG_CACHE_DIR") or os.environ.get("XDG_CACHE_HOME")
    root = Path(base).expanduser() if base else Path.home() / ".cache"
    if os.environ.get("AFG_CACHE_DIR"):
        return Path(os.environ["AFG_CACHE_DIR"]).expanduser()
    return root / "agents-fuel-gauge"


def _path(provider: str) -> Path:
    return cache_dir() / f"{provider}.json"


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


def block(provider: str, seconds: float | None) -> None:
    record = _read(provider)
    record["retry_after"] = time.time() + (seconds or DEFAULT_BACKOFF)
    _write(provider, record)


def clear() -> None:
    for path in cache_dir().glob("*.json"):
        try:
            path.unlink()
        except OSError:
            pass
