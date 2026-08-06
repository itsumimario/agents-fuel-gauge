"""Synthetic data for `--demo`.

Two jobs: let someone see what the tool looks like before they've signed in to
anything, and let the README screenshots be produced without publishing a real
account's usage. Deliberately spans the states the UI can render — normal,
warning and critical severities, a scoped per-model cap, and four of the five
pace verdicts, since a screenshot that only ever shows "on track" would
document half the tool. The fifth, `exhausted`, is left out: a meter pinned at
100% would cost a row and teach less than the 82%-with-five-days-left case,
which is the whole reason pace exists.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .models import Gauge, ProviderSnapshot

FIVE_HOURS = 5 * 3_600
ONE_WEEK = 7 * 86_400


def demo_snapshots() -> list[ProviderSnapshot]:
    now = datetime.now(timezone.utc)

    claude = ProviderSnapshot(
        key="claude",
        display_name="Claude",
        plan="Max 20x",
        account="d•••@e•••.com",
        captured_at=now,
        gauges=[
            # ~57% through a 5h window with 34% spent: comfortably under budget.
            Gauge(
                "5h", "all models", 34.0,
                now + timedelta(hours=2, minutes=11),
                window_seconds=FIVE_HOURS,
            ),
            # ~89% through the week with 68% spent: still fine.
            Gauge(
                "7d", "all models", 68.0,
                now + timedelta(hours=18, minutes=8),
                severity="warning", window_seconds=ONE_WEEK,
            ),
            # 91% spent with 89% of the week gone — the interesting case: a
            # frightening percentage that is actually on budget.
            Gauge(
                "7d", "Fable", 91.0,
                now + timedelta(hours=18, minutes=8),
                severity="critical", active_limit=True,
                window_seconds=ONE_WEEK,
            ),
        ],
    )

    codex = ProviderSnapshot(
        key="codex",
        display_name="Codex",
        plan="Pro",
        account="d•••@e•••.com",
        captured_at=now - timedelta(minutes=4),
        gauges=[
            # 82% spent with 5 days still to run: genuinely over budget.
            Gauge(
                "7d", "all models", 82.0,
                now + timedelta(days=5, hours=2),
                severity="warning",
                window_seconds=ONE_WEEK,
            ),
            Gauge(
                "7d", "GPT-5.3-Codex-Spark", 12.0,
                now + timedelta(days=6, hours=22),
                window_seconds=ONE_WEEK,
            ),
        ],
    )

    return [claude, codex]


async def fetch_demo() -> list[ProviderSnapshot]:
    """Drop-in replacement for `fetch_all` that never touches the network."""
    return demo_snapshots()
