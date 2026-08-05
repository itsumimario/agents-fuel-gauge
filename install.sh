#!/usr/bin/env bash
#
# Installer for agents-fuel-gauge (Linux).
#
#   ./install.sh                 install from this checkout
#   ./install.sh --editable      install so edits here take effect immediately
#   ./install.sh --uninstall     remove it again
#
# Installs into an isolated environment managed by `uv` and puts `afg` on your
# PATH. Nothing is installed system-wide and no sudo is required.

set -euo pipefail

REPO_URL="https://github.com/itsumimario/agents-fuel-gauge"
PACKAGE="agents-fuel-gauge"
COMMAND="afg"
EDITABLE=0
UNINSTALL=0

# --------------------------------------------------------------------------- #

BOLD=""; DIM=""; RED=""; GREEN=""; YELLOW=""; OFF=""
if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
  BOLD=$'\033[1m'; DIM=$'\033[2m'; RED=$'\033[31m'
  GREEN=$'\033[32m'; YELLOW=$'\033[33m'; OFF=$'\033[0m'
fi

say()  { printf '%s\n' "$*"; }
step() { printf '%s==>%s %s\n' "$BOLD" "$OFF" "$*"; }
warn() { printf '%s warn%s %s\n' "$YELLOW" "$OFF" "$*" >&2; }
die()  { printf '%serror%s %s\n' "$RED" "$OFF" "$*" >&2; exit 1; }
ok()   { printf '%s  ok%s %s\n' "$GREEN" "$OFF" "$*"; }

usage() {
  sed -n '3,11p' "$0" | sed 's/^# \{0,1\}//'
  exit 0
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    -e|--editable)  EDITABLE=1 ;;
    -u|--uninstall) UNINSTALL=1 ;;
    -h|--help)      usage ;;
    *)              die "unknown option: $1 (try --help)" ;;
  esac
  shift
done

# --------------------------------------------------------------------------- #
# Preconditions
# --------------------------------------------------------------------------- #

[ "$(uname -s)" = "Linux" ] || die "this installer supports Linux only (found $(uname -s))"

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)

if [ "$UNINSTALL" -eq 1 ]; then
  step "Removing $PACKAGE"
  if command -v uv >/dev/null 2>&1; then
    uv tool uninstall "$PACKAGE" 2>/dev/null || warn "$PACKAGE was not installed"
    ok "removed"
  else
    die "uv is not installed, so $PACKAGE was not installed by this script"
  fi
  exit 0
fi

[ -f "$SCRIPT_DIR/pyproject.toml" ] || die "run this from a checkout of $REPO_URL"

# --------------------------------------------------------------------------- #
# uv — the only dependency, and we can bootstrap it
# --------------------------------------------------------------------------- #

if ! command -v uv >/dev/null 2>&1; then
  step "Installing uv (Python package manager)"
  command -v curl >/dev/null 2>&1 || die "curl is required to bootstrap uv"
  curl -LsSf https://astral.sh/uv/install.sh | sh || die "uv installation failed"

  # uv lands in ~/.local/bin by default but the current shell has a stale PATH.
  for candidate in "$HOME/.local/bin" "$HOME/.cargo/bin"; do
    [ -x "$candidate/uv" ] && PATH="$candidate:$PATH"
  done
  command -v uv >/dev/null 2>&1 || die "uv installed but not on PATH; open a new shell and re-run"
  ok "uv $(uv --version 2>/dev/null | awk '{print $2}')"
else
  ok "uv $(uv --version 2>/dev/null | awk '{print $2}') already installed"
fi

# --------------------------------------------------------------------------- #
# Install
# --------------------------------------------------------------------------- #

step "Installing $PACKAGE"
if [ "$EDITABLE" -eq 1 ]; then
  uv tool install --force --editable "$SCRIPT_DIR" || die "installation failed"
  ok "installed (editable — runs from $SCRIPT_DIR)"
else
  uv tool install --force "$SCRIPT_DIR" || die "installation failed"
  ok "installed"
fi

# --------------------------------------------------------------------------- #
# Verify, and be honest about PATH
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
# Sign-in check — the tool reads whatever the official CLIs already wrote
# --------------------------------------------------------------------------- #

step "Checking for signed-in agent CLIs"
found=0
if [ -f "${CLAUDE_CONFIG_DIR:-$HOME/.claude}/.credentials.json" ]; then
  ok "Claude Code credentials found"; found=$((found + 1))
else
  warn "no Claude Code credentials — run 'claude' and sign in"
fi
if [ -f "${CODEX_HOME:-$HOME/.codex}/auth.json" ]; then
  ok "Codex credentials found"; found=$((found + 1))
else
  warn "no Codex credentials — run 'codex' and sign in"
fi

say ""
if [ "$found" -eq 0 ]; then
  warn "Nothing to report on yet. Sign in to at least one CLI, then run: $COMMAND"
else
  say "${BOLD}Done.${OFF} Try it:"
  say "  ${BOLD}$COMMAND${OFF}            live view"
  say "  ${BOLD}$COMMAND --check${OFF}    one-shot, plain text"
  say "  ${BOLD}$COMMAND --json${OFF}     one-shot, for scripts"
fi
