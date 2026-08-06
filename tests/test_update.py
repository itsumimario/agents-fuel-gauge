"""Tests for `afg --update`.

The update path can run `git pull` and reinstall, so the guard that matters
most is the one that refuses to touch a checkout with uncommitted work —
losing someone's edits to a convenience command is far worse than being one
version behind.
"""

import subprocess
from pathlib import Path

import pytest

from agents_fuel_gauge import update as update_module
from agents_fuel_gauge.update import _checkout_root, update


def git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


@pytest.fixture
def checkout(tmp_path):
    """A throwaway repo with one commit."""
    git("init", "-q", cwd=tmp_path)
    git("config", "user.email", "dev@example.com", cwd=tmp_path)
    git("config", "user.name", "dev", cwd=tmp_path)
    (tmp_path / "file.txt").write_text("v1\n")
    git("add", "-A", cwd=tmp_path)
    git("commit", "-qm", "first", cwd=tmp_path)
    return tmp_path


class TestDirtyTreeGuard:
    def test_refuses_to_update_a_dirty_checkout(self, checkout, monkeypatch, capsys):
        (checkout / "file.txt").write_text("uncommitted edit\n")
        monkeypatch.setattr(update_module, "_checkout_root", lambda: checkout)

        assert update() == 1
        out = capsys.readouterr().out
        assert "uncommitted changes" in out
        assert "not touching it" in out

    def test_dirty_checkout_is_left_completely_alone(self, checkout, monkeypatch):
        (checkout / "file.txt").write_text("precious\n")
        monkeypatch.setattr(update_module, "_checkout_root", lambda: checkout)

        update()
        assert (checkout / "file.txt").read_text() == "precious\n"

    def test_untracked_files_also_count_as_dirty(self, checkout, monkeypatch, capsys):
        (checkout / "scratch.txt").write_text("notes\n")
        monkeypatch.setattr(update_module, "_checkout_root", lambda: checkout)

        assert update() == 1
        assert "uncommitted changes" in capsys.readouterr().out


class TestCleanCheckout:
    def test_reports_failure_when_there_is_no_remote(self, checkout, monkeypatch, capsys):
        """A clean tree with no upstream should explain itself, not crash."""
        monkeypatch.setattr(update_module, "_checkout_root", lambda: checkout)

        assert update() == 1
        assert "git pull failed" in capsys.readouterr().out

    def test_already_up_to_date_is_success(self, checkout, monkeypatch, capsys):
        monkeypatch.setattr(update_module, "_checkout_root", lambda: checkout)
        # Simulate a pull that succeeds and changes nothing.
        real_run = update_module._run

        def fake_run(*args, **kwargs):
            if args[:2] == ("git", "-C") and "pull" in args:
                return subprocess.CompletedProcess(args, 0, "Already up to date.\n", "")
            return real_run(*args, **kwargs)

        monkeypatch.setattr(update_module, "_run", fake_run)
        assert update() == 0
        assert "already up to date" in capsys.readouterr().out


class TestInstallDetection:
    def test_detects_this_editable_checkout(self):
        """This test suite runs from a checkout, so detection must find it."""
        root = _checkout_root()
        assert root is not None
        assert (root / "pyproject.toml").exists()

    def test_falls_back_to_git_when_not_in_a_checkout(self, monkeypatch, capsys):
        monkeypatch.setattr(update_module, "_checkout_root", lambda: None)
        monkeypatch.setattr(update_module.shutil, "which", lambda _: None)

        assert update() == 1
        assert "uv is not installed" in capsys.readouterr().out


def test_update_flag_is_wired_up(monkeypatch):
    """--update must short-circuit before any network fetch happens."""
    from agents_fuel_gauge import cli

    called = {}

    def fake_update():
        called["yes"] = True
        return 0

    monkeypatch.setattr("agents_fuel_gauge.update.update", fake_update)
    monkeypatch.setattr(
        cli, "fetch_all", lambda: pytest.fail("--update must not fetch usage")
    )
    assert cli.main(["--update"]) == 0
    assert called


class TestVersionReporting:
    def test_reads_version_from_disk_not_memory(self, tmp_path):
        """After a pull the imported __version__ is stale by definition."""
        pkg = tmp_path / "src" / "agents_fuel_gauge"
        pkg.mkdir(parents=True)
        (pkg / "__init__.py").write_text('__version__ = "9.9.9"\n__author__ = "x"\n')
        assert update_module._version_on_disk(tmp_path) == "9.9.9"

    def test_missing_file_is_not_fatal(self, tmp_path):
        assert update_module._version_on_disk(tmp_path) is None

    def test_up_to_date_message_includes_the_version(self, checkout, monkeypatch, capsys):
        pkg = checkout / "src" / "agents_fuel_gauge"
        pkg.mkdir(parents=True)
        (pkg / "__init__.py").write_text('__version__ = "1.2.3"\n')
        subprocess.run(["git", "add", "-A"], cwd=checkout, check=True)
        subprocess.run(["git", "commit", "-qm", "add pkg"], cwd=checkout, check=True)

        monkeypatch.setattr(update_module, "_checkout_root", lambda: checkout)
        real_run = update_module._run

        def fake_run(*args, **kwargs):
            if args[:2] == ("git", "-C") and "pull" in args:
                return subprocess.CompletedProcess(args, 0, "Already up to date.\n", "")
            return real_run(*args, **kwargs)

        monkeypatch.setattr(update_module, "_run", fake_run)
        assert update() == 0
        assert "v1.2.3" in capsys.readouterr().out
