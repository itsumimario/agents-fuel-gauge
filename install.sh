#!/usr/bin/env bash
#
# Installer for agents-fuel-gauge (Linux).
#
# One-liner:
#   curl -LsSf https://raw.githubusercontent.com/itsumimario/agents-fuel-gauge/main/install.sh | bash
#
# From a checkout:
#   ./install.sh                 install
#   ./install.sh --editable      run from this checkout, so edits apply live
#   ./install.sh --uninstall     remove it
#
# Installs into an isolated environment managed by `uv`. No sudo, nothing
# system-wide, no compiler.

set -euo pipefail

REPO_URL="https://github.com/itsumimario/agents-fuel-gauge"
PACKAGE="agents-fuel-gauge"
COMMAND="afg"
EDITABLE=0
UNINSTALL=0

# --------------------------------------------------------------------------- #

BOLD=""; RED=""; GREEN=""; YELLOW=""; OFF=""
if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
  BOLD=$'\033[1m'; RED=$'\033[31m'
  GREEN=$'\033[32m'; YELLOW=$'\033[33m'; OFF=$'\033[0m'
fi

say()  { printf '%s\n' "$*"; }
step() { printf '%s==>%s %s\n' "$BOLD" "$OFF" "$*"; }
warn() { printf '%s warn%s %s\n' "$YELLOW" "$OFF" "$*" >&2; }
die()  { printf '%serror%s %s\n' "$RED" "$OFF" "$*" >&2; exit 1; }
ok()   { printf '%s  ok%s %s\n' "$GREEN" "$OFF" "$*"; }

usage() { sed -n '3,14p' "$0" | sed 's/^# \{0,1\}//'; exit 0; }

while [ "$#" -gt 0 ]; do
  case "$1" in
    -e|--editable)  EDITABLE=1 ;;
    -u|--uninstall) UNINSTALL=1 ;;
    -h|--help)      usage ;;
    *)              die "unknown option: $1 (try --help)" ;;
  esac
  shift
done

[ "$(uname -s)" = "Linux" ] || die "this installer supports Linux only (found $(uname -s))"

# When piped from curl there is no script directory to work from, so fall back
# to installing straight from git.
SOURCE_DIR=""
if [ -n "${BASH_SOURCE[0]:-}" ] && [ -f "${BASH_SOURCE[0]}" ]; then
  candidate=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
  [ -f "$candidate/pyproject.toml" ] && SOURCE_DIR="$candidate"
fi

# --------------------------------------------------------------------------- #

if [ "$UNINSTALL" -eq 1 ]; then
  step "Removing $PACKAGE"
  command -v uv >/dev/null 2>&1 || die "uv is not installed; nothing to remove"
  uv tool uninstall "$PACKAGE" 2>/dev/null || warn "$PACKAGE was not installed"
  ok "removed"
  exit 0
fi

if [ "$EDITABLE" -eq 1 ] && [ -z "$SOURCE_DIR" ]; then
  die "--editable needs a checkout; clone the repo and run ./install.sh --editable"
fi

# --------------------------------------------------------------------------- #
# uv — the only prerequisite, and it can be bootstrapped
# --------------------------------------------------------------------------- #

if ! command -v uv >/dev/null 2>&1; then
  step "Installing uv (Python package manager)"
  command -v curl >/dev/null 2>&1 || die "curl is required to bootstrap uv"
  curl -LsSf https://astral.sh/uv/install.sh | sh || die "uv installation failed"
  for candidate in "$HOME/.local/bin" "$HOME/.cargo/bin"; do
    [ -x "$candidate/uv" ] && PATH="$candidate:$PATH"
  done
  command -v uv >/dev/null 2>&1 || die "uv installed but not on PATH; open a new shell and re-run"
  ok "uv $(uv --version 2>/dev/null | awk '{print $2}')"
else
  ok "uv $(uv --version 2>/dev/null | awk '{print $2}') already installed"
fi

# --------------------------------------------------------------------------- #

step "Installing $PACKAGE"
if [ "$EDITABLE" -eq 1 ]; then
  uv tool install --force --editable "$SOURCE_DIR" || die "installation failed"
  ok "installed (editable — runs from $SOURCE_DIR)"
elif [ -n "$SOURCE_DIR" ]; then
  uv tool install --force "$SOURCE_DIR" || die "installation failed"
  ok "installed from $SOURCE_DIR"
else
  uv tool install --force "git+$REPO_URL.git" || die "installation failed"
  ok "installed from $REPO_URL"
fi

# --------------------------------------------------------------------------- #

BIN_DIR=$(uv tool dir --bin 2>/dev/null || echo "$HOME/.local/bin")

if ! command -v "$COMMAND" >/dev/null 2>&1; then
  if [ -x "$BIN_DIR/$COMMAND" ]; then
    warn "$BIN_DIR is not on your PATH."
    say  ""
    say  "  Add this to your ~/.bashrc or ~/.zshrc, then open a new shell:"
    say  "    ${BOLD}export PATH=\"$BIN_DIR:\$PATH\"${OFF}"
    say  ""
  else
    die "installation finished but $COMMAND was not found"
  fi
else
  ok "$COMMAND -> $(command -v "$COMMAND")"
fi

# --------------------------------------------------------------------------- #
# There is no login step — the tool reads whatever the official CLIs wrote.
# --------------------------------------------------------------------------- #

step "Checking for signed-in agent CLIs"
found=0
if [ -f "${CLAUDE_CONFIG_DIR:-$HOME/.claude}/.credentials.json" ]; then
  ok "Claude Code — signed in"; found=$((found + 1))
else
  warn "Claude Code — not signed in (run 'claude' once)"
fi
if [ -f "${CODEX_HOME:-$HOME/.codex}/auth.json" ]; then
  ok "Codex — signed in"; found=$((found + 1))
else
  warn "Codex — not signed in (run 'codex' once)"
fi

say ""
if [ "$found" -eq 0 ]; then
  warn "No signed-in CLIs found yet. Sign in to either one, then run: $COMMAND"
  say  "Meanwhile, see what it looks like with: ${BOLD}$COMMAND --demo${OFF}"
else
  say "${BOLD}Done.${OFF}"
  say "  ${BOLD}$COMMAND${OFF}            live dashboard"
  say "  ${BOLD}$COMMAND --check${OFF}    one-shot, plain text"
  say "  ${BOLD}$COMMAND --json${OFF}     one-shot, for scripts"
  say "  ${BOLD}$COMMAND --update${OFF}   update to the latest version"
fi
