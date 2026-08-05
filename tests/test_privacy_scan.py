"""Tests for the privacy scanner.

A scanner nobody has seen fail is indistinguishable from a scanner that always
returns "clean", so every rule is exercised against a value it must catch and a
value it must not.

All specimens below are invented. This file is on the scanner's own exemption
list precisely so it can contain rule-shaped text without tripping itself.
"""

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from privacy_scan import RULES, Finding, main, scan_text  # noqa: E402

RULE = {rule.name: rule for rule in RULES}


def hits(text: str, *, path: str = "some_file.py") -> set[str]:
    return {f.rule.name for f in scan_text(text, path)}


class TestCatchesRealLeaks:
    """Each specimen is the shape of something that must never be published."""

    @pytest.mark.parametrize(
        "rule_name, specimen",
        [
            ("email-address", "contact me at first.last@somemail.com please"),
            ("home-path", "traceback from /home/alice/project/main.py"),
            ("home-path", "/Users/bob/Library/Application Support"),
            ("machine-name", "hostname was DESKTOP-A1B2C3D4"),
            ("private-key", "-----BEGIN RSA PRIVATE KEY-----\nMIIE"),
            ("api-token", "key = sk-ant-api03-AAAAAAAAAAAAAAAAAAAA"),
            ("api-token", "token: ghp_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"),
            ("api-token", "aws: AKIAIOSFODNN7EXAMPLE"),
            ("jwt", "auth: eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdefghijk"),
            ("credential-assignment", '"refresh_token": "aVeryLongLivedValue123456"'),
            ("real-account-in-asset", "shown as f•••@m•••.private.example-corp.net"),
        ],
    )
    def test_specimen_is_flagged(self, rule_name, specimen):
        assert rule_name in hits(specimen), f"{rule_name} missed: {specimen!r}"

    def test_every_rule_has_a_specimen(self):
        """Adding a rule without a test would leave it unverified forever."""
        covered = {
            "email-address", "home-path", "machine-name", "private-key",
            "api-token", "jwt", "credential-assignment", "real-account-in-asset",
        }
        assert {rule.name for rule in RULES} == covered


class TestAllowsSafeValues:
    @pytest.mark.parametrize(
        "safe",
        [
            "write to someone@example.com",
            "noreply@anthropic.com sends nothing",
            "59209682+someuser@users.noreply.github.com",
            "the demo account is d•••@e•••.com",
            "any placeholder like a•••@e•••.com is fine",
            "CI runs under /home/runner/work",
            'token = f"Bearer {token}"',
            'tokens.get("access_token")',
            '"api_key": "your-key-here"',
            "paths render as ~/.claude/.credentials.json",
        ],
    )
    def test_safe_value_is_not_flagged(self, safe):
        assert hits(safe) == set(), f"false positive on {safe!r}"

    def test_masked_real_domain_is_still_caught(self):
        """Masking is not anonymisation: the surviving suffix identifies it."""
        assert "real-account-in-asset" in hits("a•••@p•••.somerelay.example-corp.com")


class TestRedaction:
    def test_output_never_contains_the_full_secret(self, capsys):
        secret = "ghp_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
        finding = Finding("f.py", 1, RULE["api-token"], secret)
        assert secret not in finding.redacted()
        assert len(finding.redacted()) <= len(secret)

    def test_short_values_are_fully_masked(self):
        assert set(Finding("f.py", 1, RULE["email-address"], "a@b.co").redacted()) == {"*"}


class TestScopes:
    def test_exempt_paths_are_not_scanned(self):
        leak = "real.person@somemail.com"
        assert hits(leak, path="scripts/privacy_scan.py") == set()
        assert hits(leak, path="src/app.py") != set()

    def test_lock_files_are_skipped(self):
        assert hits("x@y.com", path="uv.lock") == set()


class TestAgainstThisRepository:
    """The regression guard: this repo must stay publishable."""

    def test_repo_scans_clean(self):
        repo = Path(__file__).resolve().parent.parent
        result = subprocess.run(
            [sys.executable, "scripts/privacy_scan.py"],
            cwd=repo, capture_output=True, text=True,
        )
        assert result.returncode == 0, result.stdout + result.stderr

    def test_exit_code_is_one_when_something_is_found(self, tmp_path, monkeypatch):
        subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
        (tmp_path / "leak.txt").write_text("ping me at real.person@somemail.com\n")
        subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
        monkeypatch.chdir(tmp_path)
        assert main(["--worktree"]) == 1
