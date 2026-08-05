#!/usr/bin/env bash
#
# Point git at the repository's version-controlled hooks.
#
# `.git/hooks` is not tracked, so hooks committed there would never reach a
# clone. `core.hooksPath` moves the whole directory into the repository, which
# means the protection travels with the code and can be reviewed in a diff.

set -euo pipefail

REPO=$(git rev-parse --show-toplevel)
cd "$REPO"

chmod +x .githooks/* scripts/*.sh scripts/*.py 2>/dev/null || true
git config core.hooksPath .githooks

echo "hooks enabled: $(git config core.hooksPath)"
echo
echo "  pre-push  refuses to publish private data (scripts/privacy_scan.py)"
echo
echo "Run the scan yourself any time with:"
echo "  python3 scripts/privacy_scan.py"
echo
echo "Disable with: git config --unset core.hooksPath"
