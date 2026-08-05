"""Normalized shapes for provider quota data.

Anthropic and OpenAI describe their limits with completely different JSON, and
each vendor keeps adding scoped sub-limits (Fable's weekly cap, Codex-Spark's
weekly cap). Both get flattened into a list of `Gauge`s here so the TUI never
has to care which vendor a number came from, and so a *new* scoped limit
appearing upstream renders for free instead of needing a code change.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

WARNING_AT = 60.0
CRITICAL_AT = 85.0

SEVERITIES = ("normal", "warning", "critical")


def derive_severity(percent: float) -> str:
    """Fallback for providers that do not grade their own limits (Codex)."""
    if percent >= CRITICAL_AT:
        return "critical"
    if percent >= WARNING_AT:
        return "warning"
    return "normal"


def format_countdown(seconds: int | None) -> str:
    """Coarse at a distance, precise up close — seconds only in the last hour."""
    if seconds is None:
        return ""
    days, rem = divmod(int(seconds), 86_400)
    hours, rem = divmod(rem, 3_600)
    minutes, secs = divmod(rem, 60)
    if days:
        return f"{days}d {hours:02d}h"
    if hours:
        return f"{hours}h {minutes:02d}m"
    return f"{minutes}m {secs:02d}s"


def format_age(captured_at: datetime | None, now: datetime | None = None) -> str:
    """How long ago a snapshot was taken, for per-provider freshness."""
    if captured_at is None:
        return "never"
    now = now or datetime.now(timezone.utc)
    seconds = max(0, int((now - captured_at).total_seconds()))
    if seconds < 5:
        return "just now"
    if seconds < 60:
        return f"{seconds}s ago"
    if seconds < 3_600:
        return f"{seconds // 60}m ago"
    if seconds < 86_400:
        return f"{seconds // 3_600}h ago"
    return f"{seconds // 86_400}d ago"


def mask_email(email: str | None) -> str | None:
    """`someone@example.com` -> `s•••@e•••.com`, so screenshots stay shareable."""
    if not email or "@" not in email:
        return email
    local, _, domain = email.partition("@")
    head, _, rest = domain.partition(".")
    if not local or not head or not rest:
        return email
    return f"{local[0]}•••@{head[0]}•••.{rest}"


@dataclass(frozen=True)
class Gauge:
    """One quota bar: a window (5h/7d) crossed with a scope (all models/Fable)."""

    window: str
    scope: str
    percent: float
    resets_at: datetime | None = None
    severity: str = "normal"
    runs_out_first: bool = False
    """The limit that will actually stop you before any of the others do.

    Anthropic reports this directly as `is_active`; OpenAI does not, so for
    Codex it is inferred from whichever bar is fullest.
    """

    @property
    def label(self) -> str:
        return f"{self.window} {self.scope}"

    def seconds_remaining(self, now: datetime | None = None) -> int | None:
        if self.resets_at is None:
            return None
        now = now or datetime.now(timezone.utc)
        return max(0, int((self.resets_at - now).total_seconds()))


@dataclass
class ProviderSnapshot:
    """What one agent CLI's quota looks like at a moment in time."""

    key: str
    display_name: str
    gauges: list[Gauge] = field(default_factory=list)
    plan: str | None = None
    account: str | None = None
    captured_at: datetime | None = None
    error: str | None = None
    stale: bool = False
    """True when `gauges` were carried over from an earlier, successful poll."""

    @property
    def ok(self) -> bool:
        return self.error is None

    def carry_forward(self, previous: "ProviderSnapshot | None") -> "ProviderSnapshot":
        """Keep showing the last good numbers when a poll fails.

        A transient 429 or a dropped connection should not blank the panel —
        stale data with a visible warning is far more useful than nothing.
        """
        if self.ok or previous is None or not previous.gauges:
            return self
        self.gauges = previous.gauges
        self.plan = self.plan or previous.plan
        self.account = self.account or previous.account
        # Age must describe the data, not the failed attempt — otherwise a
        # panel full of hours-old numbers would claim to be "just now".
        self.captured_at = previous.captured_at
        self.stale = True
        return self

    @property
    def subtitle(self) -> str:
        return " · ".join(p for p in (self.plan, self.account) if p)

    @property
    def worst(self) -> Gauge | None:
        return max(self.gauges, key=lambda g: g.percent, default=None)

    def to_dict(self) -> dict:
        return {
            "provider": self.key,
            "displayName": self.display_name,
            "plan": self.plan,
            "account": self.account,
            "capturedAt": self.captured_at.isoformat() if self.captured_at else None,
            "error": self.error,
            "stale": self.stale,
            "gauges": [
                {
                    "window": g.window,
                    "scope": g.scope,
                    "label": g.label,
                    "percent": g.percent,
                    "severity": g.severity,
                    "runsOutFirst": g.runs_out_first,
                    "resetsAt": g.resets_at.isoformat() if g.resets_at else None,
                    "secondsRemaining": g.seconds_remaining(),
                }
                for g in self.gauges
            ],
        }
