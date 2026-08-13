"""Parsing tests built from real payload shapes.

The whole value of this tool is reading the *right* fields, so these lock in
the two things most likely to break: that scoped per-model limits come out of
`limits[]`, and that the dead `seven_day_*` keys are never trusted.
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agents_fuel_gauge.models import (
    ProviderSnapshot,
    format_age,
    format_countdown,
    format_remaining,
    mask_email,
)
from agents_fuel_gauge.sources import (
    SourceError,
    _cached_get,
    _claude_gauges,
    _codex_gauges,
    _codex_plan_name,
    _duration_label,
    _plan_from_tier,
    _read_json,
    claude_credentials_path,
    codex_auth_path,
    tidy_path,
)


class _NoRequestClient:
    async def get(self, *args, **kwargs):
        pytest.fail("a standing provider backoff must not make a request")


async def test_cached_get_explains_stale_data_during_backoff(monkeypatch):
    monkeypatch.setattr("agents_fuel_gauge.sources.cache.load", lambda *args: (None, 90))
    monkeypatch.setattr(
        "agents_fuel_gauge.sources.cache.load_stale",
        lambda *args: ({"usage": 42}, 3_601),
    )
    monkeypatch.setattr(
        "agents_fuel_gauge.sources.cache.blocked_for", lambda *args: 125.9
    )

    payload, age, warning = await _cached_get(
        _NoRequestClient(), "claude", "https://example.invalid", {}, 60
    )

    assert payload == {"usage": 42}
    assert age == 3_601
    assert warning == "rate limited — retrying in 125s"


async def test_forced_refresh_probes_past_a_stale_backoff(monkeypatch):
    stored = []

    class LiveResponse:
        status_code = 200
        headers = {}

        @staticmethod
        def json():
            return {"usage": 0}

    class LiveClient:
        calls = 0

        async def get(self, *args, **kwargs):
            self.calls += 1
            return LiveResponse()

    monkeypatch.setattr(
        "agents_fuel_gauge.sources.cache.load", lambda *args: (None, 90)
    )
    monkeypatch.setattr(
        "agents_fuel_gauge.sources.cache.blocked_for", lambda *args: 125.9
    )
    monkeypatch.setattr(
        "agents_fuel_gauge.sources.cache.store",
        lambda provider, payload: stored.append((provider, payload)),
    )
    client = LiveClient()

    payload, age, warning = await _cached_get(
        client, "claude", "https://example.invalid", {}, 0
    )

    assert client.calls == 1
    assert (payload, age, warning) == ({"usage": 0}, 0.0, None)
    assert stored == [("claude", {"usage": 0})]

# Trimmed from a live GET /api/oauth/usage response.
CLAUDE_PAYLOAD = {
    "five_hour": {"utilization": 4.0, "resets_at": "2026-08-05T23:10:00.183278+00:00"},
    "seven_day": {"utilization": 50.0, "resets_at": "2026-08-06T16:00:00.183307+00:00"},
    # Present but dead on current accounts — must never be read.
    "seven_day_opus": None,
    "seven_day_sonnet": None,
    "seven_day_omelette": None,
    "limits": [
        {
            "kind": "session",
            "percent": 4,
            "severity": "normal",
            "resets_at": "2026-08-05T23:10:00.183278+00:00",
            "scope": None,
            "is_active": False,
        },
        {
            "kind": "weekly_all",
            "percent": 50,
            "severity": "normal",
            "resets_at": "2026-08-06T16:00:00.183307+00:00",
            "scope": None,
            "is_active": False,
        },
        {
            "kind": "weekly_scoped",
            "percent": 91,
            "severity": "critical",
            "resets_at": "2026-08-06T16:00:00.183626+00:00",
            "scope": {"model": {"id": None, "display_name": "Fable"}, "surface": None},
            "is_active": True,
        },
    ],
}

# Trimmed from a live GET /backend-api/wham/usage response.
CODEX_PAYLOAD = {
    "plan_type": "pro",
    "rate_limit": {
        "primary_window": {
            "used_percent": 98,
            "limit_window_seconds": 604800,
            "reset_at": 1786159937,
        },
        "secondary_window": None,
    },
    "additional_rate_limits": [
        {
            "limit_name": "GPT-5.3-Codex-Spark",
            "rate_limit": {
                "primary_window": {
                    "used_percent": 0,
                    "limit_window_seconds": 604800,
                    "reset_at": 1786571402,
                },
                "secondary_window": None,
            },
        }
    ],
}


class TestClaude:
    def test_reads_every_limit_including_scoped(self):
        gauges = _claude_gauges(CLAUDE_PAYLOAD)
        assert [g.label for g in gauges] == [
            "5h all models",
            "7d all models",
            "7d Fable",
        ]

    def test_scoped_model_keeps_severity_and_first_flag(self):
        fable = next(g for g in _claude_gauges(CLAUDE_PAYLOAD) if g.scope == "Fable")
        assert fable.percent == 91
        assert fable.severity == "critical"
        assert fable.active_limit is True

    def test_only_one_gauge_active_limit(self):
        gauges = _claude_gauges(CLAUDE_PAYLOAD)
        assert sum(g.active_limit for g in gauges) == 1

    def test_resets_at_is_timezone_aware(self):
        gauge = _claude_gauges(CLAUDE_PAYLOAD)[0]
        assert gauge.resets_at is not None
        assert gauge.resets_at.utcoffset() == timezone.utc.utcoffset(None)

    def test_falls_back_to_flat_windows_when_limits_absent(self):
        payload = {k: v for k, v in CLAUDE_PAYLOAD.items() if k != "limits"}
        gauges = _claude_gauges(payload)
        assert [g.label for g in gauges] == ["5h all models", "7d all models"]
        assert [g.percent for g in gauges] == [4.0, 50.0]

    def test_null_legacy_per_model_keys_never_produce_a_gauge(self):
        # A tracker reading seven_day_opus/omelette would emit phantom 0% bars.
        gauges = _claude_gauges(CLAUDE_PAYLOAD)
        assert not any("opus" in g.scope.lower() for g in gauges)
        assert not any("omelette" in g.scope.lower() for g in gauges)


class TestCodex:
    def test_reads_primary_and_scoped_limits(self):
        gauges = _codex_gauges(CODEX_PAYLOAD)
        assert [g.label for g in gauges] == [
            "7d all models",
            "7d GPT-5.3-Codex-Spark",
        ]

    def test_absent_secondary_window_is_skipped(self):
        assert all(g.window == "7d" for g in _codex_gauges(CODEX_PAYLOAD))

    def test_severity_is_derived_since_openai_does_not_grade(self):
        gauges = _codex_gauges(CODEX_PAYLOAD)
        assert gauges[0].severity == "critical"  # 98%
        assert gauges[1].severity == "normal"  # 0%

    def test_codex_flags_nothing_since_openai_reports_nothing(self):
        """Guessing "the fullest bar" reads days-old usage as current."""
        gauges = _codex_gauges(CODEX_PAYLOAD)
        assert all(g.active_limit is False for g in gauges)

    def test_empty_payload_yields_nothing(self):
        assert _codex_gauges({}) == []


class TestCarryForward:
    def _good(self):
        return ProviderSnapshot(
            key="claude",
            display_name="Claude",
            plan="Max 20x",
            gauges=_claude_gauges(CLAUDE_PAYLOAD),
        )

    def test_failed_snapshot_inherits_previous_gauges(self):
        failed = ProviderSnapshot(key="claude", display_name="Claude", error="boom")
        merged = failed.carry_forward(self._good())
        assert merged.stale is True
        assert merged.error == "boom"
        assert [g.label for g in merged.gauges] == [
            "5h all models", "7d all models", "7d Fable",
        ]
        assert merged.plan == "Max 20x"

    def test_successful_snapshot_is_untouched(self):
        fresh = self._good()
        assert fresh.carry_forward(self._good()) is fresh
        assert fresh.stale is False

    def test_nothing_to_inherit_stays_empty(self):
        failed = ProviderSnapshot(key="claude", display_name="Claude", error="boom")
        merged = failed.carry_forward(None)
        assert merged.gauges == []
        assert merged.stale is False


class TestCredentialPaths:
    """Overrides matter for anyone whose CLIs are not in the default place."""

    def test_claude_defaults_to_home(self, monkeypatch):
        monkeypatch.delenv("AFG_CLAUDE_CREDENTIALS", raising=False)
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        assert claude_credentials_path() == Path.home() / ".claude" / ".credentials.json"

    def test_claude_honours_official_config_dir(self, monkeypatch):
        monkeypatch.delenv("AFG_CLAUDE_CREDENTIALS", raising=False)
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/opt/cc")
        assert claude_credentials_path() == Path("/opt/cc/.credentials.json")

    def test_our_override_wins_over_the_official_one(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/opt/cc")
        monkeypatch.setenv("AFG_CLAUDE_CREDENTIALS", "/tmp/creds.json")
        assert claude_credentials_path() == Path("/tmp/creds.json")

    def test_codex_honours_codex_home(self, monkeypatch):
        monkeypatch.delenv("AFG_CODEX_AUTH", raising=False)
        monkeypatch.setenv("CODEX_HOME", "/opt/cx")
        assert codex_auth_path() == Path("/opt/cx/auth.json")

    def test_tidy_path_hides_the_home_directory(self):
        assert tidy_path(Path.home() / ".claude" / "x.json") == "~/.claude/x.json"
        assert tidy_path(Path("/etc/hosts")) == "/etc/hosts"

    def test_no_message_leaks_an_absolute_home_path(self, monkeypatch, tmp_path):
        missing = tmp_path / "nope.json"
        monkeypatch.setenv("AFG_CLAUDE_CREDENTIALS", str(missing))
        with pytest.raises(SourceError) as exc:
            _read_json(claude_credentials_path(), "claude")
        assert str(Path.home()) not in str(exc.value)


class TestAge:
    def test_age_buckets(self):
        now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        at = lambda **kw: format_age(now - timedelta(**kw), now)  # noqa: E731
        assert at(seconds=1) == "just now"
        assert at(seconds=30) == "30s ago"
        assert at(minutes=5) == "5m ago"
        assert at(hours=3) == "3h ago"
        assert at(days=2) == "2d ago"
        assert format_age(None) == "never"

    def test_carry_forward_keeps_the_original_capture_time(self):
        """Stale data must not claim to be fresh."""
        old = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        previous = ProviderSnapshot(
            key="claude", display_name="Claude",
            gauges=_claude_gauges(CLAUDE_PAYLOAD), captured_at=old,
        )
        failed = ProviderSnapshot(
            key="claude", display_name="Claude", error="boom",
            captured_at=datetime(2026, 1, 1, 18, 0, 0, tzinfo=timezone.utc),
        )
        assert failed.carry_forward(previous).captured_at == old


class TestHelpers:
    def test_duration_label(self):
        assert _duration_label(604800) == "7d"
        assert _duration_label(18000) == "5h"
        assert _duration_label(300) == "5m"
        assert _duration_label(None) == "?"

    def test_plan_from_tier(self):
        assert _plan_from_tier("default_claude_max_20x") == "Max 20x"
        assert _plan_from_tier("default_claude_pro") == "Pro"
        assert _plan_from_tier(None) is None


class TestCodexPlanNames:
    """Codex answers with a plan family; the product has a longer name.

    `pro` and `prolite` are two different subscriptions — ChatGPT Pro 20x and
    Pro 5x — and rendering both as "Pro" loses the distinction the user is
    paying for.
    """

    def test_the_two_pro_tiers_are_distinguished(self):
        assert _codex_plan_name("pro") == "Pro 20x"
        assert _codex_plan_name("prolite") == "Pro 5x"

    def test_simple_families_keep_their_name(self):
        assert _codex_plan_name("plus") == "Plus"
        assert _codex_plan_name("go") == "Go"
        assert _codex_plan_name("team") == "Team"

    def test_the_several_enterprise_spellings_all_land_on_one_name(self):
        for raw in ("enterprise", "ent26", "enterprise_cbp_usage_based"):
            assert _codex_plan_name(raw) == "Enterprise"

    def test_unknown_plans_are_tidied_not_dropped_or_mislabelled(self):
        """A plan invented next quarter must not be renamed to one we know."""
        assert _codex_plan_name("pro_50x") == "Pro 50X"
        assert _codex_plan_name("some_new_thing") == "Some New Thing"

    def test_missing_or_malformed_yields_nothing(self):
        assert _codex_plan_name(None) is None
        assert _codex_plan_name("") is None
        assert _codex_plan_name("   ") is None
        assert _codex_plan_name(7) is None

    def test_case_and_padding_are_ignored(self):
        assert _codex_plan_name("  PRO  ") == "Pro 20x"

    def test_countdown_granularity(self):
        assert format_countdown(2 * 86400 + 5 * 3600) == "2d 05h"
        assert format_countdown(18 * 3600 + 8 * 60) == "18h 08m"
        assert format_countdown(75) == "1m 15s"
        assert format_countdown(None) == ""

    def test_remaining_granularity(self):
        """Minutes matter inside a day and are noise past one."""
        assert format_remaining(3 * 3600 + 42 * 60) == "3h 42m"
        assert format_remaining(3 * 3600 + 5 * 60) == "3h 05m"
        assert format_remaining(23 * 3600 + 59 * 60) == "23h 59m"
        assert format_remaining(5 * 86400 + 3600 + 42 * 60) == "5d 1h"
        assert format_remaining(42 * 60) == "42m"
        assert format_remaining(30) == "<1m"
        assert format_remaining(None) == ""

    def test_remaining_minutes_are_padded_so_the_column_stays_flush(self):
        """Right-aligned, "3h 5m" and "18h 42m" would jag against each other."""
        short = format_remaining(3 * 3600 + 5 * 60)
        long = format_remaining(18 * 3600 + 42 * 60)
        assert len(short) + 1 == len(long)  # differs only by the hours digit

    def test_mask_email(self):
        assert mask_email("someone@example.com") == "s•••@e•••.com"
        assert mask_email(None) is None
        assert mask_email("not-an-email") == "not-an-email"
