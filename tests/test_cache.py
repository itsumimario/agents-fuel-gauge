"""Cross-process request coordination and durable rate-limit state."""

from __future__ import annotations

import asyncio
import multiprocessing
import os
import time

from agents_fuel_gauge import cache
from agents_fuel_gauge.sources import RateLimited, _cached_get


class _SuccessfulResponse:
    status_code = 200
    headers = {}

    @staticmethod
    def json():
        return {"usage": 42}


class _RateLimitedResponse:
    status_code = 429
    headers = {}


class _SuccessfulClient:
    async def get(self, *args, **kwargs):
        return _SuccessfulResponse()


def _concurrent_fetch(
    cache_dir, ready, start, calls, results, max_age=300, response_status=200
):
    """Process entrypoint: wait at one gate, then use the public fetch path."""
    os.environ["AFG_CACHE_DIR"] = cache_dir

    class CountedClient:
        async def get(self, *args, **kwargs):
            with calls.get_lock():
                calls.value += 1
            time.sleep(0.2)
            return (
                _SuccessfulResponse()
                if response_status == 200
                else _RateLimitedResponse()
            )

    ready.release()
    start.wait(timeout=5)
    try:
        payload, _, _ = asyncio.run(
            _cached_get(
                CountedClient(), "claude", "https://example.invalid", {}, max_age
            )
        )
        results.put(payload)
    except RateLimited as exc:
        results.put(str(exc))


def _run_concurrently(tmp_path, *, max_age=300, response_status=200):
    context = multiprocessing.get_context("fork")
    ready = context.Semaphore(0)
    start = context.Event()
    calls = context.Value("i", 0)
    results = context.Queue()
    processes = [
        context.Process(
            target=_concurrent_fetch,
            args=(
                str(tmp_path),
                ready,
                start,
                calls,
                results,
                max_age,
                response_status,
            ),
        )
        for _ in range(2)
    ]

    for process in processes:
        process.start()
    for _ in processes:
        assert ready.acquire(timeout=5)
    start.set()
    for process in processes:
        process.join(timeout=5)
        assert process.exitcode == 0

    return calls.value, [results.get(timeout=1) for _ in processes]


def test_concurrent_processes_share_one_upstream_request(tmp_path):
    """Two dashboards crossing an expiry boundary must be single-flight."""
    calls, results = _run_concurrently(tmp_path)

    assert calls == 1
    assert results == [
        {"usage": 42},
        {"usage": 42},
    ]


def test_concurrent_forced_refreshes_share_one_upstream_request(tmp_path):
    """Two users pressing refresh together should still issue one probe."""
    calls, results = _run_concurrently(tmp_path, max_age=0)

    assert calls == 1
    assert results == [
        {"usage": 42},
        {"usage": 42},
    ]


def test_concurrent_forced_refreshes_share_one_429(tmp_path):
    """A waiting manual refresh must not amplify the first caller's 429."""
    calls, warnings = _run_concurrently(
        tmp_path, max_age=0, response_status=429
    )

    assert calls == 1
    assert all("rate limited" in warning for warning in warnings)


async def test_later_forced_refresh_still_makes_a_new_request(tmp_path, monkeypatch):
    """Single-flight must coalesce overlap, not suppress a later key press."""
    class CountedClient:
        calls = 0

        async def get(self, *args, **kwargs):
            self.calls += 1
            return _SuccessfulResponse()

    monkeypatch.setenv("AFG_CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(cache.time, "time", lambda: 1_000.0)
    client = CountedClient()

    await _cached_get(client, "claude", "https://example.invalid", {}, 0)
    await _cached_get(client, "claude", "https://example.invalid", {}, 0)

    assert client.calls == 2


async def test_unavailable_lock_directory_degrades_to_an_uncached_fetch(
    tmp_path, monkeypatch
):
    unusable = tmp_path / "not-a-directory"
    unusable.write_text("occupied")
    monkeypatch.setenv("AFG_CACHE_DIR", str(unusable))

    payload, age, warning = await _cached_get(
        _SuccessfulClient(),
        "claude",
        "https://example.invalid",
        {},
        300,
    )

    assert (payload, age, warning) == ({"usage": 42}, 0.0, None)


def test_repeated_429_after_success_increases_backoff_and_retains_cause(
    tmp_path, monkeypatch
):
    """A brief success must not reset a repeating rate-limit incident."""
    clock = [1_000.0]
    monkeypatch.setenv("AFG_CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(cache.time, "time", lambda: clock[0])
    monkeypatch.setattr(cache.os, "getpid", lambda: 4321)

    cache.block("claude", None, forced=False)
    assert cache.blocked_for("claude") == 120
    assert cache.rate_limit_status("claude") == {
        "at": 1_000.0,
        "seconds": 120.0,
        "pid": 4321,
        "mode": "automatic",
        "source": "adaptive",
        "level": 1,
    }

    clock[0] += 60
    cache.store("claude", {"usage": 42})
    assert cache.blocked_for("claude") == 0
    assert cache.rate_limit_status("claude")["level"] == 1

    clock[0] += 60
    cache.block("claude", None, forced=False)
    assert cache.blocked_for("claude") == 240
    assert cache.rate_limit_status("claude") == {
        "at": 1_120.0,
        "seconds": 240.0,
        "pid": 4321,
        "mode": "automatic",
        "source": "adaptive",
        "level": 2,
    }

    for expected, level in ((480, 3), (960, 4), (1_920, 5), (3_600, 6)):
        clock[0] += 60
        cache.store("claude", {"usage": 42})
        clock[0] += 60
        cache.block("claude", None, forced=False)
        assert cache.blocked_for("claude") == expected
        assert cache.rate_limit_status("claude")["level"] == level

    clock[0] += 60
    cache.block("claude", None, forced=False)
    assert cache.blocked_for("claude") == 3_600
    assert cache.rate_limit_status("claude")["level"] == 6

    cache.block("codex", 900, forced=True)
    assert cache.blocked_for("codex") == 900
    assert cache.rate_limit_status("codex")["source"] == "server"
    assert cache.rate_limit_status("codex")["mode"] == "manual"
