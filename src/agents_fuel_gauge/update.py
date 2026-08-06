"""`afg --update` — pull the latest version and reinstall.

Two ways this tool gets installed, and they update differently:

* **editable** — the command runs straight out of a git checkout, so updating
  means `git pull` in that checkout. No reinstall is needed unless dependencies
  changed, which a reinstall handles cheaply anyway.
* **copied** — `uv tool install` took a snapshot, so the source of truth is the
  git remote and the fix is to reinstall from it.

Which one you have is detectable: for an editable install this module still
lives inside a git working tree.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

REPO_URL = "https://github.com/itsumimario/agents-fuel-gauge.git"
PACKAGE = "agents-fuel-gauge"


def _run(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        args, cwd=cwd, capture_output=True, text=True, errors="replace"
    )


def _checkout_root() -> Path | None:
    """The git working tree this module lives in, if any."""
    here = Path(__file__).resolve().parent
    result = _run("git", "-C", str(here), "rev-parse", "--show-toplevel")
    if result.returncode != 0:
        return None
    root = Path(result.stdout.strip())
    return root if root.is_dir() else None


def _update_checkout(root: Path) -> int:
    # Never touch a dirty tree — losing someone's uncommitted work to a
    # convenience command is a far worse outcome than being one version behind.
    dirty = _run("git", "-C", str(root), "status", "--porcelain").stdout.strip()
    if dirty:
        print(f"! {root} has uncommitted changes; not touching it.")
        print("  Commit or stash them, then run `afg --update` again.")
        return 1

    before = _run("git", "-C", str(root), "rev-parse", "--short", "HEAD").stdout.strip()
    print(f"updating {root} …")

    pull = _run("git", "-C", str(root), "pull", "--ff-only")
    if pull.returncode != 0:
        print("! git pull failed:")
        print("  " + (pull.stderr.strip() or pull.stdout.strip()))
        print("  Your branch may have diverged from the remote.")
        return 1

    after = _run("git", "-C", str(root), "rev-parse", "--short", "HEAD").stdout.strip()
    if before == after:
        print(f"already up to date ({after})")
        return 0

    print(f"updated {before} -> {after}")
    if shutil.which("uv"):
        # Picks up any dependency changes; harmless when there are none.
        install = _run("uv", "tool", "install", "--force", "--editable", str(root))
        if install.returncode != 0:
            print("! reinstall failed; the code is updated but dependencies may not be:")
            print("  " + (install.stderr.strip() or install.stdout.strip()))
            return 1
    print("done — run `afg --version` to confirm")
    return 0


def _reinstall_from_git() -> int:
    if not shutil.which("uv"):
        print("! uv is not installed, so this copy cannot update itself.")
        print(f"  Reinstall manually from {REPO_URL}")
        return 1

    print(f"reinstalling {PACKAGE} from {REPO_URL} …")
    result = _run("uv", "tool", "install", "--force", f"git+{REPO_URL}")
    if result.returncode != 0:
        print("! update failed:")
        print("  " + (result.stderr.strip() or result.stdout.strip()))
        return 1
    print("done — run `afg --version` to confirm")
    return 0


def update() -> int:
    root = _checkout_root()
    if root is not None:
        return _update_checkout(root)
    return _reinstall_from_git()
