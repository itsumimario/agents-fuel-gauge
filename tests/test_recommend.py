"""The Stellate-facing Sol/Opus recommendation contract."""

import json
from datetime import datetime, timedelta, timezone

import pytest

from agents_fuel_gauge.cli import main
from agents_fuel_gauge.models import Gauge, ProviderSnapshot
from agents_fuel_gauge.recommend import (
    RecommendationUnavailable,
    parse_duration,
    recommend_minion,
)


NOW = datetime(2026, 8, 15, 12, 0, tzinfo=timezone.utc)
WEEK = 7 * 86_400


def gauge(
    percent: float,
    elapsed: float,
    *,
    scope: str = "all models",
    active: bool = False,
) -> Gauge:
    remaining = WEEK * (1.0 - elapsed)
    return Gauge(
        "7d",
        scope,
        percent,
        NOW + timedelta(seconds=remaining),
        active_limit=active,
        window_seconds=WEEK,
    )


def snapshots(
    *,
    sol: Gauge | None = None,
    opus: Gauge | None = None,
) -> list[ProviderSnapshot]:
    return [
        ProviderSnapshot(
            key="claude",
            display_name="Claude",
            captured_at=NOW,
            gauges=[opus or gauge(40, 0.5)],
        ),
        ProviderSnapshot(
            key="codex",
            display_name="Codex",
            captured_at=NOW,
            gauges=[sol or gauge(30, 0.5)],
        ),
    ]


class TestDuration:
    @pytest.mark.parametrize(
        ("text", "seconds"),
        [("45m", 2_700), ("2h", 7_200), ("1h30m", 5_400), ("1.5d", 129_600)],
    )
    def test_compact_forms(self, text, seconds):
        assert parse_duration(text) == seconds

    @pytest.mark.parametrize("text", ["", "90", "2 hours", "0m", "h2", "1h nope"])
    def test_rejects_ambiguous_or_nonpositive_values(self, text):
        with pytest.raises(ValueError):
            parse_duration(text)


class TestRecommendation:
    def test_sol_wins_when_it_is_healthier(self):
        result = recommend_minion(snapshots(), now=NOW)

        assert result.recommendation == "sol"

    def test_opus_wins_when_sol_is_materially_more_pressured(self):
        data = snapshots(sol=gauge(60, 0.5), opus=gauge(20, 0.5))

        result = recommend_minion(data, now=NOW)

        assert result.recommendation == "opus-5"

    def test_near_ties_prefer_sol(self):
        data = snapshots(sol=gauge(30, 0.5), opus=gauge(27.5, 0.5))

        result = recommend_minion(data, now=NOW)

        assert result.recommendation == "sol"
        assert "preferred" in result.reason

    def test_effort_controls_how_readily_opus_overrides_the_preference(self):
        data = snapshots(sol=gauge(30, 0.5), opus=gauge(26, 0.5))

        assert recommend_minion(data, effort="low", now=NOW).recommendation == "sol"
        assert recommend_minion(data, effort="medium", now=NOW).recommendation == "sol"
        assert recommend_minion(data, effort="high", now=NOW).recommendation == "opus-5"

    def test_duration_discounts_a_constraint_that_resets_during_the_run(self):
        sol = Gauge(
            "5h",
            "all models",
            75,
            NOW + timedelta(minutes=30),
            window_seconds=5 * 3_600,
        )
        data = snapshots(sol=sol, opus=gauge(30, 0.5))

        assert recommend_minion(data, now=NOW).recommendation == "opus-5"
        result = recommend_minion(data, duration_seconds=2 * 3_600, now=NOW)
        assert result.recommendation == "sol"
        sol_meter = result.candidates["sol"].meters[0]
        assert sol_meter.exposure == pytest.approx(0.25)

    def test_projected_pace_can_outweigh_a_slightly_lower_raw_percentage(self):
        # Sol has more percent left, but it burned that amount in only 20% of
        # its week.  The existing workload is already on course to exhaust it.
        data = snapshots(sol=gauge(30, 0.2), opus=gauge(35, 0.8))

        result = recommend_minion(data, now=NOW)

        assert result.recommendation == "opus-5"
        assert result.candidates["sol"].pressure == pytest.approx(150)

    def test_active_claude_scope_is_conservatively_applied_to_opus(self):
        claude = ProviderSnapshot(
            key="claude",
            display_name="Claude",
            captured_at=NOW,
            gauges=[
                gauge(20, 0.5),
                gauge(90, 0.5, scope="Fable", active=True),
            ],
        )
        codex = ProviderSnapshot(
            key="codex",
            display_name="Codex",
            captured_at=NOW,
            gauges=[gauge(40, 0.5)],
        )

        result = recommend_minion([claude, codex], now=NOW)

        assert result.recommendation == "sol"
        assert [m.applicability for m in result.candidates["opus-5"].meters] == [
            "all-models",
            "provider-active-scope",
        ]

    def test_unrelated_codex_scope_does_not_penalize_sol(self):
        codex = ProviderSnapshot(
            key="codex",
            display_name="Codex",
            captured_at=NOW,
            gauges=[
                gauge(20, 0.5),
                gauge(99, 0.5, scope="GPT-5.3-Codex-Spark"),
            ],
        )
        result = recommend_minion([snapshots()[0], codex], now=NOW)

        assert result.recommendation == "sol"
        assert [m.scope for m in result.candidates["sol"].meters] == ["all models"]

    def test_stale_cache_is_usable_with_a_warning(self):
        data = snapshots()
        data[1].stale = True
        data[1].error = "rate limited (429)"
        data[1].captured_at = NOW - timedelta(hours=2)

        result = recommend_minion(data, now=NOW)

        assert result.recommendation == "sol"
        assert result.candidates["sol"].data_usable is True
        assert result.to_dict()["stale"] is True
        assert any("stale cached" in warning for warning in result.warnings)

    def test_one_unavailable_provider_selects_the_other(self):
        absent = ProviderSnapshot(
            key="claude",
            display_name="Claude",
            installed=False,
            error="Claude CLI is not installed or not on PATH",
        )

        result = recommend_minion([absent, snapshots()[1]], now=NOW)

        assert result.recommendation == "sol"
        assert "only available" in result.reason

    def test_exhausted_candidate_is_never_recommended(self):
        data = snapshots(sol=gauge(100, 0.5), opus=gauge(70, 0.5))

        result = recommend_minion(data, now=NOW)

        assert result.recommendation == "opus-5"
        assert result.candidates["sol"].status == "quota_exhausted"

    def test_neither_candidate_fails_instead_of_guessing(self):
        unavailable = [
            ProviderSnapshot(key="claude", display_name="Claude", error="signed out"),
            ProviderSnapshot(key="codex", display_name="Codex", error="signed out"),
        ]

        with pytest.raises(RecommendationUnavailable) as caught:
            recommend_minion(unavailable, now=NOW)

        assert caught.value.to_dict()["error"]["code"] == "no_usable_candidate"

    def test_json_contract_names_only_supported_recommendations(self):
        payload = recommend_minion(snapshots(), now=NOW).to_dict()

        assert payload["schema"] == "afg.minion-recommendation/v1"
        assert payload["recommendation"] in {"sol", "opus-5"}
        assert (payload["vendor"], payload["model"]) == ("codex", "gpt-5.6-sol")
        assert set(payload["candidates"]) == {"sol", "opus-5"}


class TestRecommendationCli:
    def test_plain_stdout_is_exactly_one_stable_enum(self, capsys):
        assert main(["--demo", "--recommend-minion"]) == 0

        captured = capsys.readouterr()
        assert captured.out == "sol\n"
        assert captured.err == ""

    def test_json_is_structured_recommendation_not_the_normal_usage_envelope(
        self, capsys
    ):
        assert main(["--demo", "--recommend-minion", "--json", "--effort", "high"]) == 0

        payload = json.loads(capsys.readouterr().out)
        assert payload["recommendation"] in {"sol", "opus-5"}
        assert payload["effort"] == "high"
        assert "providers" not in payload

    def test_stale_warning_goes_to_stderr_without_polluting_stdout(
        self, monkeypatch, capsys
    ):
        data = snapshots()
        data[1].stale = True
        data[1].error = "rate limited (429)"

        async def stale(max_age=0):
            return data

        monkeypatch.setattr("agents_fuel_gauge.cli.fetch_all", stale)
        assert main(["--recommend-minion"]) == 0

        captured = capsys.readouterr()
        assert captured.out == "sol\n"
        assert "warning:" in captured.err
        assert "stale cached" in captured.err

    def test_machine_failure_is_json_and_nonzero(self, monkeypatch, capsys):
        async def broken(max_age=0):
            return []

        monkeypatch.setattr("agents_fuel_gauge.cli.fetch_all", broken)
        assert main(["--recommend-minion", "--json"]) == 1

        payload = json.loads(capsys.readouterr().out)
        assert payload["error"]["code"] == "no_usable_candidate"
        assert "recommendation" not in payload

    def test_invalid_duration_fails_before_fetching(self, monkeypatch):
        def explode(*args, **kwargs):
            raise AssertionError("invalid input must not fetch provider data")

        monkeypatch.setattr("agents_fuel_gauge.cli.fetch_all", explode)
        with pytest.raises(SystemExit) as caught:
            main(["--recommend-minion", "--duration", "a while"])
        assert caught.value.code == 2

    def test_duration_and_effort_are_scoped_to_recommendation_mode(self):
        with pytest.raises(SystemExit) as caught:
            main(["--duration", "2h"])
        assert caught.value.code == 2
