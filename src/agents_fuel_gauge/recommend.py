"""Choose the healthier subscription for additional work.

This deliberately answers one narrow question: whether the user's current
subscription headroom makes Sol or Opus 5 the safer place for *additional*
work.  It does not pretend to estimate how many tokens an arbitrary task will
consume, or that one percent of two different vendors' plans represents the
same number of tokens.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .models import Gauge, ProviderSnapshot


SCHEMA = "afg.placement-recommendation/v1"

# Sol is the default when the choices are close.  More consequential work gets
# a smaller margin, so a modestly healthier Opus subscription can win before a
# long/high-effort task is committed to the preferred provider.
EFFORT_MARGINS = {
    "low": 20.0,
    "medium": 10.0,
    "high": 5.0,
}


@dataclass(frozen=True)
class CandidateSpec:
    name: str
    provider: str
    model: str
    cli_model: str
    scope_tokens: tuple[str, ...]


CANDIDATES = {
    "sol": CandidateSpec(
        "sol", "codex", "gpt-5.6-sol", "gpt-5.6-sol", ("sol",)
    ),
    "opus-5": CandidateSpec(
        "opus-5", "claude", "opus-5", "claude-opus-5", ("opus",)
    ),
}


def parse_duration(value: str) -> float:
    """Parse a compact positive duration such as ``45m`` or ``1h30m``."""
    text = value.strip().lower()
    if not text:
        raise ValueError("duration cannot be empty")

    units = {"s": 1.0, "m": 60.0, "h": 3_600.0, "d": 86_400.0, "w": 604_800.0}
    position = 0
    seconds = 0.0
    for match in re.finditer(r"(\d+(?:\.\d+)?)\s*([smhdw])", text):
        if match.start() != position:
            raise ValueError(
                "duration must use s, m, h, d, or w (for example 45m or 1h30m)"
            )
        seconds += float(match.group(1)) * units[match.group(2)]
        position = match.end()
    if position != len(text):
        raise ValueError(
            "duration must use s, m, h, d, or w (for example 45m or 1h30m)"
        )
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError("duration must be greater than zero")
    return seconds


def _scope_matches(gauge: Gauge, spec: CandidateSpec) -> tuple[bool, str | None]:
    if not gauge.scoped:
        return True, "all-models"

    words = set(re.findall(r"[a-z0-9]+", gauge.scope.lower()))
    if any(token in words for token in spec.scope_tokens):
        return True, "model-scope"

    # Claude tells us which scoped cap is currently in force, even when the
    # public label is a codename rather than "Opus".  Codex exposes no
    # equivalent flag, and we refuse to guess that a Spark cap applies to Sol.
    if spec.provider == "claude" and gauge.active_limit:
        return True, "provider-active-scope"
    return False, None


@dataclass(frozen=True)
class MeterAssessment:
    label: str
    scope: str
    percent: float
    projected_percent: float | None
    pressure: float
    seconds_remaining: int | None
    exposure: float
    basis: str
    applicability: str
    exhausted: bool

    def to_dict(self) -> dict:
        return {
            "label": self.label,
            "scope": self.scope,
            "percent": round(self.percent, 1),
            "projectedUsagePercent": (
                None
                if self.projected_percent is None
                else round(self.projected_percent, 1)
            ),
            "pressure": None if math.isinf(self.pressure) else round(self.pressure, 1),
            "secondsRemaining": self.seconds_remaining,
            "durationExposure": round(self.exposure, 3),
            "basis": self.basis,
            "applicability": self.applicability,
            "exhausted": self.exhausted,
        }


@dataclass
class CandidateAssessment:
    spec: CandidateSpec
    installed: bool = False
    data_usable: bool = False
    available: bool = False
    stale: bool = False
    captured_at: datetime | None = None
    pressure: float | None = None
    status: str = "unavailable"
    meters: list[MeterAssessment] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self, now: datetime) -> dict:
        age = None
        if self.captured_at is not None:
            age = max(0, int((now - self.captured_at).total_seconds()))
        return {
            "provider": self.spec.provider,
            "model": self.spec.model,
            # `model` is AFG's stable vocabulary. This is the exact identifier
            # the vendor CLI accepts, so launchers never have to guess or keep
            # their own opus-5 -> claude-opus-5 translation table.
            "cli_model": self.spec.cli_model,
            "installed": self.installed,
            "dataUsable": self.data_usable,
            "available": self.available,
            "status": self.status,
            "stale": self.stale,
            "capturedAt": (
                self.captured_at.isoformat() if self.captured_at is not None else None
            ),
            "dataAgeSeconds": age,
            "pressure": (
                None
                if self.pressure is None or math.isinf(self.pressure)
                else round(self.pressure, 1)
            ),
            "meters": [meter.to_dict() for meter in self.meters],
            "warnings": self.warnings,
        }


@dataclass(frozen=True)
class PlacementRecommendation:
    recommendation: str
    effort: str
    expected_duration_seconds: float | None
    switch_margin: float
    reason: str
    candidates: dict[str, CandidateAssessment]
    warnings: tuple[str, ...]
    at: datetime

    @property
    def spec(self) -> CandidateSpec:
        return CANDIDATES[self.recommendation]

    def to_dict(self) -> dict:
        return {
            "schema": SCHEMA,
            "at": self.at.isoformat(),
            "recommendation": self.recommendation,
            "vendor": self.spec.provider,
            "model": self.spec.model,
            "cli_model": self.spec.cli_model,
            "effort": self.effort,
            "expectedDurationSeconds": (
                None
                if self.expected_duration_seconds is None
                else round(self.expected_duration_seconds, 3)
            ),
            "switchMarginPoints": self.switch_margin,
            "method": "worst reset-adjusted quota pressure",
            "reason": self.reason,
            "stale": any(candidate.stale for candidate in self.candidates.values()),
            "warnings": list(self.warnings),
            "candidates": {
                name: candidate.to_dict(self.at)
                for name, candidate in self.candidates.items()
            },
        }


class RecommendationUnavailable(RuntimeError):
    """No supported candidate has both usable data and quota available."""

    def __init__(
        self,
        message: str,
        candidates: dict[str, CandidateAssessment],
        warnings: tuple[str, ...],
        at: datetime,
    ) -> None:
        super().__init__(message)
        self.candidates = candidates
        self.warnings = warnings
        self.at = at

    def to_dict(self) -> dict:
        return {
            "schema": SCHEMA,
            "at": self.at.isoformat(),
            "error": {
                "code": "no_usable_candidate",
                "message": str(self),
            },
            "warnings": list(self.warnings),
            "candidates": {
                name: candidate.to_dict(self.at)
                for name, candidate in self.candidates.items()
            },
        }


def _meter_assessment(
    gauge: Gauge,
    applicability: str,
    duration_seconds: float | None,
    now: datetime,
) -> MeterAssessment:
    pace = gauge.pace(now)
    remaining = gauge.seconds_remaining(now)
    exhausted = gauge.percent >= 100.0

    projected = None
    base_pressure = max(0.0, gauge.percent)
    basis = "utilization"
    if pace is not None and pace.verdict == "window_over":
        # An expired stale sample describes the previous window, not current
        # capacity.  Keep it in the evidence but give it no vote.
        base_pressure = 0.0
        basis = "expired-window"
        exhausted = False
    elif pace is not None and pace.actionable:
        projected = pace.ratio * 100.0
        base_pressure = max(base_pressure, projected)
        basis = "utilization-and-projected-pace"
    elif pace is not None:
        basis = "utilization-only-unjudgeable-pace"
    else:
        basis = "utilization-only-no-reset-timing"

    exposure = 1.0
    if duration_seconds is not None and remaining is not None:
        exposure = min(1.0, max(0.0, remaining) / duration_seconds)
    pressure = 0.0 if exposure == 0.0 else base_pressure * exposure

    return MeterAssessment(
        label=gauge.label,
        scope=gauge.scope,
        percent=gauge.percent,
        projected_percent=projected,
        pressure=pressure,
        seconds_remaining=remaining,
        exposure=exposure,
        basis=basis,
        applicability=applicability,
        exhausted=exhausted,
    )


def _assess_candidate(
    spec: CandidateSpec,
    snapshot: ProviderSnapshot | None,
    duration_seconds: float | None,
    now: datetime,
) -> CandidateAssessment:
    assessment = CandidateAssessment(spec=spec)
    if snapshot is None:
        assessment.status = "provider_missing"
        assessment.warnings.append(f"{spec.provider} did not return a provider record")
        return assessment

    assessment.installed = snapshot.installed
    assessment.stale = snapshot.stale
    assessment.captured_at = snapshot.captured_at
    if not snapshot.installed:
        assessment.status = "not_installed"
        assessment.warnings.append(f"{spec.provider} CLI is not installed")
        return assessment

    if snapshot.error:
        assessment.warnings.append(f"{spec.provider}: {snapshot.error}")
    if snapshot.stale:
        assessment.warnings.append(
            f"{spec.provider} recommendation uses stale cached quota data"
        )

    for gauge in snapshot.gauges:
        applies, applicability = _scope_matches(gauge, spec)
        if applies and applicability is not None:
            assessment.meters.append(
                _meter_assessment(
                    gauge, applicability, duration_seconds=duration_seconds, now=now
                )
            )

    if not assessment.meters:
        assessment.status = "no_applicable_meters"
        assessment.warnings.append(
            f"{spec.provider} returned no quota meter applicable to {spec.name}"
        )
        return assessment

    assessment.data_usable = True
    assessment.pressure = max(meter.pressure for meter in assessment.meters)
    if any(meter.exhausted for meter in assessment.meters):
        assessment.status = "quota_exhausted"
        assessment.available = False
        assessment.warnings.append(f"{spec.name} has an exhausted applicable quota")
        return assessment

    assessment.available = True
    assessment.status = "stale" if snapshot.stale else "ready"
    return assessment


def recommend_placement(
    snapshots: list[ProviderSnapshot],
    *,
    duration_seconds: float | None = None,
    effort: str = "medium",
    now: datetime | None = None,
) -> PlacementRecommendation:
    """Recommend ``sol`` or ``opus-5`` from normalized provider snapshots."""
    if effort not in EFFORT_MARGINS:
        raise ValueError(f"unknown effort {effort!r}")
    if duration_seconds is not None and duration_seconds <= 0:
        raise ValueError("duration must be greater than zero")

    now = now or datetime.now(timezone.utc)
    by_provider = {snapshot.key: snapshot for snapshot in snapshots}
    assessments = {
        name: _assess_candidate(
            spec,
            by_provider.get(spec.provider),
            duration_seconds=duration_seconds,
            now=now,
        )
        for name, spec in CANDIDATES.items()
    }
    warnings = tuple(
        warning
        for assessment in assessments.values()
        for warning in assessment.warnings
    )
    available = {
        name: assessment
        for name, assessment in assessments.items()
        if assessment.available
    }
    if not available:
        raise RecommendationUnavailable(
            "neither Sol nor Opus 5 has usable, available quota data",
            assessments,
            warnings,
            now,
        )

    if len(available) == 1:
        choice = next(iter(available))
        other = "opus-5" if choice == "sol" else "sol"
        reason = f"{choice} is the only available candidate; {other} is unavailable"
    else:
        sol_pressure = assessments["sol"].pressure
        opus_pressure = assessments["opus-5"].pressure
        assert sol_pressure is not None and opus_pressure is not None
        margin = EFFORT_MARGINS[effort]
        if opus_pressure + margin < sol_pressure:
            choice = "opus-5"
            reason = (
                f"opus-5 quota pressure is {sol_pressure - opus_pressure:.1f} points "
                f"lower than sol, exceeding the {margin:.1f}-point {effort}-effort "
                "switch margin"
            )
        else:
            choice = "sol"
            reason = (
                "Sol is preferred because Opus 5 is not more than "
                f"{margin:.1f} quota-pressure points healthier"
            )

    return PlacementRecommendation(
        recommendation=choice,
        effort=effort,
        expected_duration_seconds=duration_seconds,
        switch_margin=EFFORT_MARGINS[effort],
        reason=reason,
        candidates=assessments,
        warnings=warnings,
        at=now,
    )
