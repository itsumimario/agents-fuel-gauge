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

# How far the projected finish may drift from budget before it is worth acting
# on. Quota use is bursty, so a tight band would flap between verdicts every
# poll and be useless as a control signal.
PACE_TOLERANCE = 0.15

# A rate multiplier is meaningless once it gets large — "go 400x faster" is
# noise from a near-zero denominator, not advice.
MAX_RATE_ADJUSTMENT = 10.0

# Early in a window the elapsed fraction is tiny, so any usage at all divides
# into a huge ratio: ten minutes into a week, one request "projects" to
# thousands of percent. That is arithmetically true and completely useless, and
# as a control signal it is actively harmful — it would tell a scheduler to
# throttle to a crawl on three hours of evidence. Below this much elapsed there
# is no rate worth estimating, so say so instead of guessing.
MIN_ELAPSED_FRACTION = 0.05


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


def format_reset_at(when: datetime | None, style: str = "full") -> str:
    """The wall-clock moment a window resets, in local time.

    A duration alone ("1d 16h") is hard to plan around; an absolute time is
    what you compare against your calendar. Both are shown, because either one
    alone leaves you doing arithmetic.
    """
    if when is None:
        return ""
    local = when.astimezone()
    if style == "full":
        return f"{local:%a} {local:%b} {local.day} {local:%H:%M}"
    if style == "short":
        return f"{local:%b} {local.day} {local:%H:%M}"
    return f"{local:%H:%M}"


def format_remaining(seconds: int | None) -> str:
    """Days and hours, or just hours inside a day, or minutes inside an hour.

    Coarser than `format_countdown` on purpose: the exact reset time is shown
    beside it, so this only has to answer "roughly how long have I got".
    """
    if seconds is None:
        return ""
    total = int(seconds)
    days, rem = divmod(total, 86_400)
    hours, rem = divmod(rem, 3_600)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h"
    minutes = rem // 60
    return f"{minutes}m" if minutes else "<1m"


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


def directives(
    snapshots: list["ProviderSnapshot"], now: datetime | None = None
) -> list[dict]:
    """One instruction per meter — never one instruction for all of them.

    An earlier version ranked every gauge together and emitted a single
    "runs out first" winner with one rate multiplier. That was wrong twice
    over. Ranking across providers implies a prediction the data cannot
    support: `used / elapsed` is the *average rate so far*, so a meter that
    burned hard days ago and has been idle since looks exactly like one
    burning steadily right now. And a lone multiplier reads as advice about
    everything you are running, when it only ever described one window.

    Each entry here names its own meter and applies only to that meter.
    """
    out: list[dict] = []
    for snapshot in snapshots:
        for gauge in snapshot.gauges:
            pace = gauge.pace(now)
            if pace is None:
                continue
            out.append(
                {
                    "provider": snapshot.key,
                    "label": gauge.label,
                    "scope": gauge.scope,
                    "window": gauge.window,
                    "percent": gauge.percent,
                    "severity": gauge.severity,
                    "verdict": pace.verdict,
                    "actionable": pace.actionable,
                    "rateAdjustment": round(pace.rate_adjustment, 3),
                    "advice": pace.advice,
                    "projectedUsagePercent": (
                        None
                        if pace.ratio == float("inf")
                        else round(pace.ratio * 100, 1)
                    ),
                    "resetsAt": gauge.resets_at.isoformat() if gauge.resets_at else None,
                    "secondsRemaining": gauge.seconds_remaining(now),
                }
            )
    return out


@dataclass(frozen=True)
class Pace:
    """Whether consumption is on track to last the window, and what to do.

    The headline percentage cannot answer the question people actually have.
    "91% used" is fine with an hour left and a crisis with four days left. Pace
    compares how much of the *budget* is gone against how much of the *window*
    is gone.

    `ratio` is the projected share of budget consumed by reset time, so 1.0 is
    landing exactly on empty and 2.4 means needing 240% of what you have.

    `rate_adjustment` is the actionable number and is deliberately *not*
    `1 / ratio`. It looks forward from now: the factor to multiply the current
    consumption rate by, over the time that remains, to finish exactly at 100%.
    A scheduler can apply it directly. 0.28 means "throttle to 28% of your
    current rate"; 1.9 means "there is headroom for nearly double".
    """

    elapsed_fraction: float
    ratio: float
    verdict: str  # "exhausted" | "slow_down" | "on_track" | "spare_capacity"
    rate_adjustment: float
    exhausts_in_seconds: int | None
    exhausts_before_reset: bool

    ACTIONABLE = ("exhausted", "slow_down", "on_track", "spare_capacity")

    @property
    def actionable(self) -> bool:
        return self.verdict in self.ACTIONABLE

    @property
    def advice(self) -> str:
        """Always says "its average rate", never "current rate".

        The rate is inferred from `used / elapsed` across the whole window, so
        it is an average, not a live measurement — a meter used heavily days ago
        and idle since reports the same figure as one burning steadily now.
        Wording that claims otherwise would overstate what one snapshot knows.
        """
        return {
            "exhausted": "spent; wait for its reset",
            "slow_down": f"slow this meter to {self.rate_adjustment:.0%} of its average rate",
            "on_track": "this meter's average rate lasts to its reset",
            "spare_capacity": f"room for {self.rate_adjustment:.1f}x this meter's average rate",
            "too_early": "window too new to judge this meter",
        }[self.verdict]

    def to_dict(self) -> dict:
        return {
            "verdict": self.verdict,
            "ratio": round(self.ratio, 3),
            "projectedUsagePercent": round(self.ratio * 100, 1),
            "rateAdjustment": round(self.rate_adjustment, 3),
            "elapsedPercent": round(self.elapsed_fraction * 100, 1),
            "exhaustsInSeconds": self.exhausts_in_seconds,
            "exhaustsBeforeReset": self.exhausts_before_reset,
            "advice": self.advice,
        }


@dataclass(frozen=True)
class Gauge:
    """One quota bar: a window (5h/7d) crossed with a scope (all models/Fable)."""

    window: str
    scope: str
    percent: float
    resets_at: datetime | None = None
    severity: str = "normal"
    active_limit: bool = False
    window_seconds: int | None = None
    """Length of the window. Carried from the provider rather than parsed back
    out of the `window` label, which would break on any format we didn't
    anticipate."""
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

    def pace(self, now: datetime | None = None) -> Pace | None:
        """Project this window's finish, and say how to change course.

        Returns None when the provider gave us too little to reason about — an
        absent reset time or window length. Guessing would produce a confident
        number with nothing behind it, which is worse than no number.

        Assumes usage accrues roughly linearly and that the window opened
        exactly `window_seconds` before it resets. Good enough to steer by;
        a burst right before reset will still surprise you.
        """
        remaining = self.seconds_remaining(now)
        if remaining is None or not self.window_seconds:
            return None

        window = float(self.window_seconds)
        elapsed = window - remaining
        if elapsed <= 0:
            return None  # window just opened; no rate exists yet

        elapsed_fraction = min(1.0, elapsed / window)
        used = self.percent / 100.0

        if self.percent >= 100:
            return Pace(elapsed_fraction, float("inf"), "exhausted", 0.0, 0, True)

        if used <= 0:
            # Nothing spent: the whole budget is available, but there is no
            # rate to scale, so report headroom without inventing a multiplier.
            return Pace(
                elapsed_fraction, 0.0, "spare_capacity",
                MAX_RATE_ADJUSTMENT, None, False,
            )

        # Projected share of budget consumed by reset, if the current rate holds.
        ratio = used / elapsed_fraction

        current_rate = used / elapsed
        if remaining > 0:
            required_rate = (1.0 - used) / remaining
            adjustment = min(MAX_RATE_ADJUSTMENT, required_rate / current_rate)
        else:
            adjustment = 0.0

        seconds_to_empty = int((1.0 - used) / current_rate)
        exhausts_before_reset = seconds_to_empty < remaining

        if elapsed_fraction < MIN_ELAPSED_FRACTION:
            # Not enough of the window has run to infer a rate. Advise no
            # change (1.0 is a safe no-op multiplier) rather than a number
            # derived from a near-zero denominator.
            return Pace(
                elapsed_fraction=elapsed_fraction,
                ratio=ratio,
                verdict="too_early",
                rate_adjustment=1.0,
                exhausts_in_seconds=None,
                exhausts_before_reset=False,
            )

        if ratio > 1.0 + PACE_TOLERANCE:
            verdict = "slow_down"
        elif ratio < 1.0 - PACE_TOLERANCE:
            verdict = "spare_capacity"
        else:
            verdict = "on_track"

        return Pace(
            elapsed_fraction=elapsed_fraction,
            ratio=ratio,
            verdict=verdict,
            rate_adjustment=adjustment,
            exhausts_in_seconds=seconds_to_empty if exhausts_before_reset else None,
            exhausts_before_reset=exhausts_before_reset,
        )


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

    @property
    def tightest(self) -> Gauge | None:
        """The gauge under the most pace pressure, not merely the fullest.

        A bar at 95% with an hour left is comfortable; one at 40% on day one is
        not. Ranking by projected overshoot answers "what should I change?"
        where ranking by percentage answers "what looks alarming?".
        """
        paced = [(g, g.pace()) for g in self.gauges]
        candidates = [(g, p) for g, p in paced if p is not None and p.actionable]
        if not candidates:
            return self.worst
        return max(candidates, key=lambda pair: pair[1].ratio)[0]

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
                    "activeLimit": g.active_limit,
                    "resetsAt": g.resets_at.isoformat() if g.resets_at else None,
                    "secondsRemaining": g.seconds_remaining(),
                    "windowSeconds": g.window_seconds,
                    "pace": p.to_dict() if (p := g.pace()) else None,
                }
                for g in self.gauges
            ],
        }
