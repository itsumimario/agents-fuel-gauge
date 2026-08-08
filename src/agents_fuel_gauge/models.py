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

# The scope a gauge carries when it covers everything you run, as opposed to a
# per-model sub-cap. Which side of that line a meter sits on decides what it can
# constrain: an unscoped meter governs all work, a scoped one governs only work
# in its scope. See `governing_indexes`.
ALL_MODELS = "all models"

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

# The arrow points the way you should move, not the way the number is moving.
# An earlier version pointed the other way — up meant "you are running hot" —
# which is a status report, and a status report is exactly what an arrow is bad
# at. Nobody reads a glyph and thinks "that describes my drift"; they read it as
# an instruction. So it is one: down means ease off, up means there is room.
#
# Arrows are reserved for the verdicts that have a direction. The rest used to
# get glyphs too — `·` for on-pace and `◦` for too-new — and at a normal
# terminal font size those two are all but indistinguishable, while neither
# says anything on its own. They are spelled out instead; see `Pace.display`.
# `✗` survives because it is unmistakable and marks the one state you cannot
# afford to overlook.
PACE_ARROW = {
    "slow_down": "↓",
    "spare_capacity": "↑",
    "exhausted": "✗",
    "on_track": "",
    "too_early": "",
}


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
    """Days and hours, or hours and minutes inside a day, or minutes.

    Precision is spent where a decision hangs on it. Inside a day, "3h" and
    "3h 58m" are an hour apart, and an hour is the difference between starting
    a piece of work and not. Past a day the minutes are noise — nobody plans
    Tuesday around whether the reset lands at 10:05 or 10:47 — so `5d 1h` keeps
    the column narrow for a number read at a glance.

    Minutes are zero-padded so the column stays flush when right-aligned:
    unpadded, "3h 5m" and "18h 42m" jag against each other.
    """
    if seconds is None:
        return ""
    total = int(seconds)
    days, rem = divmod(total, 86_400)
    hours, rem = divmod(rem, 3_600)
    minutes = rem // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes:02d}m"
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


def governing_indexes(
    snapshot: "ProviderSnapshot", now: datetime | None = None
) -> set[int]:
    """Which of a provider's meters actually constrain you.

    Meters are not independent, and treating them as if they were produced
    advice that was worse than useless. One request spends from *every* meter
    at once, so a 5-hour window with plenty of headroom cannot license spending
    that a nearly-empty weekly window forbids — yet per-meter advice cheerfully
    said "speed up by 150%" on the 5h row while the 7d row said "slow down by
    93%". Following the first would blow the second.

    The fix rests on one fact about `rate_adjustment`: unlike a percentage, it
    is comparable across meters. Percentages are fractions of different budgets
    — 1% of a 5-hour allowance is not 1% of a week's — but every adjustment is a
    multiplier on the *same* underlying quantity, your request throughput.
    Multiply throughput by k and every meter's consumption rate scales by k. So
    the largest multiplier you can apply without overrunning anything is simply
    the smallest adjustment among the meters that cover the work.

    Scope decides which meters cover which work:

      * unscoped meters (`all models`) constrain everything;
      * a scoped meter constrains only its own model, so its pool is the
        unscoped meters plus itself.

    A meter governs when it holds the minimum for some pool. In practice that
    is one unscoped meter — the tightest — plus any scoped meter tighter still.
    Everything else is slack, and slack is not an instruction.

    Meters too new to judge are excluded: they have no rate to compare. If that
    leaves nothing to compare at all, every meter with a reading speaks for
    itself rather than the panel falling silent.
    """
    readings: list[tuple[int, Gauge, Pace]] = []
    for index, gauge in enumerate(snapshot.gauges):
        pace = gauge.pace(now)
        if pace is not None:
            readings.append((index, gauge, pace))

    judged = [r for r in readings if r[2].actionable]
    if not judged:
        # Nothing can be ranked, so nothing is suppressed.
        return {index for index, _, _ in readings}

    def tightest(pool):
        return min(pool, key=lambda r: r[2].rate_adjustment)[0]

    general = [r for r in judged if not r[1].scoped]
    governors: set[int] = set()
    if general:
        governors.add(tightest(general))
    for reading in judged:
        if reading[1].scoped:
            governors.add(tightest(general + [reading]))
    return governors


def directives(
    snapshots: list["ProviderSnapshot"], now: datetime | None = None
) -> list[dict]:
    """One entry per meter, each flagged with whether it actually governs.

    An earlier version ranked every gauge together and emitted a single
    "runs out first" winner with one rate multiplier. That was wrong twice
    over. Ranking across providers implies a prediction the data cannot
    support: `used / elapsed` is the *average rate so far*, so a meter that
    burned hard days ago and has been idle since looks exactly like one
    burning steadily right now. And a lone multiplier reads as advice about
    everything you are running, when it only ever described one window.

    So every entry still names its own meter. But a meter's reading and a
    meter's *instruction* are different things: readings are independent,
    instructions are not, because one request spends from every meter at once.
    `governs` marks the entries whose advice can be acted on directly, and
    `effectiveRateAdjustment` gives every entry the multiplier that actually
    applies to its work once the other meters get a vote. A subscriber that
    scales its rate should use the effective figure, or filter to `governs`.
    See `governing_indexes`.
    """
    out: list[dict] = []
    for snapshot in snapshots:
        governors = governing_indexes(snapshot, now)
        # The multiplier that survives every meter covering this one's work.
        paces = [(g, g.pace(now)) for g in snapshot.gauges]
        judged = [(g, p) for g, p in paces if p is not None and p.actionable]
        general = [p.rate_adjustment for g, p in judged if not g.scoped]

        for index, gauge in enumerate(snapshot.gauges):
            pace = gauge.pace(now)
            if pace is None:
                continue
            pool = list(general)
            if pace.actionable:
                pool.append(pace.rate_adjustment)
            effective = min(pool) if pool else pace.rate_adjustment
            held_by = None
            if index not in governors:
                held_by = next(
                    (
                        g.label
                        for i, g in enumerate(snapshot.gauges)
                        if i in governors and not g.scoped
                    ),
                    None,
                )
            out.append(
                {
                    "provider": snapshot.key,
                    "label": gauge.label,
                    "scope": gauge.scope,
                    "window": gauge.window,
                    "percent": gauge.percent,
                    "severity": gauge.severity,
                    # True when this meter holds the tightest constraint on the
                    # work it covers. False means its own advice is slack that
                    # another meter overrules.
                    "governs": index in governors,
                    "heldBy": held_by,
                    "effectiveRateAdjustment": round(effective, 3),
                    "verdict": pace.verdict,
                    "actionable": pace.actionable,
                    "direction": pace.direction,
                    "rateAdjustment": round(pace.rate_adjustment, 3),
                    # The same instruction as a signed change, which is what the
                    # arrow on screen shows: -93 means ease off by 93%.
                    "changePercent": (
                        None
                        if pace.change_percent is None
                        else round(
                            pace.change_percent
                            * (-1 if pace.direction == "down" else 1),
                            1,
                        )
                    ),
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
    def arrow(self) -> str:
        return PACE_ARROW.get(self.verdict, " ")

    @property
    def direction(self) -> str:
        """Which way to move: `down`, `up`, or `hold`."""
        if self.verdict in ("slow_down", "exhausted"):
            return "down"
        if self.verdict == "spare_capacity":
            return "up"
        return "hold"

    @property
    def change_percent(self) -> float | None:
        """How far to move the rate, as a percentage of itself.

        Deliberately a *change*, not the multiplier. "Slow to 7% of your average
        rate" and "slow down by 93%" are the same instruction, but only the
        second one can be read at a glance next to an arrow — the first makes
        you subtract from 100 before you know which way to go.

        None where no change is called for, so a caller can tell "no advice"
        from "advice of zero".
        """
        if self.verdict == "slow_down":
            return (1.0 - self.rate_adjustment) * 100.0
        if self.verdict == "spare_capacity":
            return (self.rate_adjustment - 1.0) * 100.0
        return None

    @property
    def at_cap(self) -> bool:
        """True when the multiplier hit `MAX_RATE_ADJUSTMENT` and is a floor.

        Happens on a barely-touched meter, where the honest answer is "far more
        room than you are using" rather than any particular number.
        """
        return self.rate_adjustment >= MAX_RATE_ADJUSTMENT

    @property
    def change_label(self) -> str:
        """The short magnitude drawn beside the arrow: `by 93%`."""
        change = self.change_percent
        if change is None:
            return {
                "exhausted": "spent",
                "on_track": "on pace",
                "too_early": "too new",
            }.get(self.verdict, "")
        return f"by {change:.0f}%{'+' if self.at_cap else ''}"

    @property
    def display(self) -> str:
        """What the end of a row prints: an arrow with its size, or a word.

        A glyph earns its place only when it carries a direction you can act
        on. `·` for on-pace and `◦` for too-new carried none, and at a normal
        terminal font size they are near-identical — two states that mean
        opposite things about whether the reading can be trusted, told apart by
        a pixel. Words cost a few columns and are unambiguous at any size.

        `exhausted` keeps its `✗` *and* gets the word, because it is the one
        state where being overlooked costs you something.
        """
        return f"{self.arrow} {self.change_label}".strip()

    @property
    def advice(self) -> str:
        """Always says "its average rate", never "current rate".

        The rate is inferred from `used / elapsed` across the whole window, so
        it is an average, not a live measurement — a meter used heavily days ago
        and idle since reports the same figure as one burning steadily now.
        Wording that claims otherwise would overstate what one snapshot knows.
        """
        # Both readings, because they answer different questions: the change is
        # what you act on, the multiplier is what a scheduler multiplies by.
        if self.verdict == "slow_down":
            return (
                f"slow this meter down {self.change_label} — to "
                f"{self.rate_adjustment:.0%} of its average rate"
            )
        if self.verdict == "spare_capacity":
            return (
                f"this meter has room to speed up {self.change_label} — to "
                f"{self.rate_adjustment:.1f}x its average rate"
            )
        return {
            "exhausted": "spent; wait for its reset",
            "on_track": "this meter's average rate lasts to its reset",
            "too_early": "window too new to judge this meter",
        }[self.verdict]

    def to_dict(self) -> dict:
        return {
            "verdict": self.verdict,
            "direction": self.direction,
            "ratio": round(self.ratio, 3),
            "projectedUsagePercent": round(self.ratio * 100, 1),
            "rateAdjustment": round(self.rate_adjustment, 3),
            "changePercent": (
                None if self.change_percent is None else round(self.change_percent, 1)
            ),
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
    """The provider says this limit is the one currently in force.

    Anthropic reports it directly as `is_active`; OpenAI reports nothing of the
    kind, so Codex gauges always carry False. Reported, never inferred. It shows
    up as the word ACTIVE in `--check` and as `activeLimit` in `--json`; the TUI
    no longer draws a symbol for it, because a symbol nobody can decode is worse
    than no symbol.
    """
    window_seconds: int | None = None
    """Length of the window. Carried from the provider rather than parsed back
    out of the `window` label, which would break on any format we didn't
    anticipate."""

    @property
    def label(self) -> str:
        return f"{self.window} {self.scope}"

    @property
    def scoped(self) -> bool:
        """True for a per-model sub-cap, false for a meter covering everything.

        The distinction is what makes cross-meter reasoning possible: an
        unscoped meter constrains every request, a scoped one constrains only
        requests to that model. A weekly Fable cap at 96% says nothing about
        whether you can keep using Sonnet.
        """
        return self.scope != ALL_MODELS

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
