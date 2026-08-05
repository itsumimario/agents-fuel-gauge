"""Synthetic data for `--demo`.

Two jobs: let someone see what the tool looks like before they've signed in to
anything, and let the README screenshots be generated without publishing a real
account's usage. Deliberately covers every state the UI can render — normal,
warning, critical, a scoped per-model cap, and a provider that failed but is
still showing carried-forward numbers.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .models import Gauge, ProviderSnapshot


def demo_snapshots() -> list[ProviderSnapshot]:
    now = datetime.now(timezone.utc)

    claude = ProviderSnapshot(
        key="claude",
        display_name="Claude",
        plan="Max 20x",
        account="d•••@e•••.com",
        captured_at=now,
        gauges=[
            Gauge("5h", "all models", 34.0, now + timedelta(hours=2, minutes=11)),
            Gauge(
                "7d", "all models", 68.0,
                now + timedelta(hours=18, minutes=8), severity="warning",
            ),
            Gauge(
                "7d", "Fable", 91.0,
                now + timedelta(hours=18, minutes=8),
                severity="critical", runs_out_first=True,
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
            Gauge(
                "7d", "all models", 82.0,
                now + timedelta(days=2, hours=5), severity="warning",
                runs_out_first=True,
            ),
            Gauge("7d", "GPT-5.3-Codex-Spark", 12.0, now + timedelta(days=6, hours=23)),
        ],
    )

    return [claude, codex]


async def fetch_demo() -> list[ProviderSnapshot]:
    """Drop-in replacement for `fetch_all` that never touches the network."""
    return demo_snapshots()
