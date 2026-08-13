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
            # 80% through a 5h window with 18% spent: acres of burst headroom,
            # and none of it spendable, because the week below is overdrawn.
            # This row exists to show what the panel does *not* say.
            Gauge(
                "5h", "all models", 18.0,
                now + timedelta(hours=1, minutes=2),
                window_seconds=FIVE_HOURS,
            ),
            # 88% spent with two days still to run: genuinely over budget, and
            # the meter that governs everything.
            Gauge(
                "7d", "all models", 88.0,
                now + timedelta(days=2, minutes=8),
                severity="critical", window_seconds=ONE_WEEK,
            ),
            # A per-model cap tighter still, so it governs Fable work on top of
            # the general limit — two instructions, each true of its own scope.
            Gauge(
                "7d", "Fable", 91.0,
                now + timedelta(days=2, minutes=8),
                severity="critical", active_limit=True,
                window_seconds=ONE_WEEK,
            ),
        ],
    )

    codex = ProviderSnapshot(
        key="codex",
        display_name="Codex",
        # The mapped product name, not the raw `pro` the API answers with —
        # the screenshot should show what a real panel shows.
        plan="Pro 20x",
        account="d•••@e•••.com",
        captured_at=now - timedelta(minutes=4),
        gauges=[
            # 62% spent with a day left: room to go faster, and nothing else
            # here to forbid it, so this one does get to say so.
            Gauge(
                "7d", "all models", 62.0,
                now + timedelta(days=1, hours=3),
                severity="warning",
                window_seconds=ONE_WEEK,
            ),
            # A scoped cap whose window has barely opened: no rate to estimate,
            # so it cannot govern, but its "too new" status stays visible.
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
