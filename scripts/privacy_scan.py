#!/usr/bin/env python3
"""Refuse to publish private data.

Scans the working tree, the staged changes, and every commit reachable in the
repository — file contents, commit messages, and author/committer identities —
for things that should never reach a public repository.

    python3 scripts/privacy_scan.py              # worktree + full history
    python3 scripts/privacy_scan.py --staged     # staged changes only (fast)
    python3 scripts/privacy_scan.py --worktree   # working tree only
    python3 scripts/privacy_scan.py --history    # every commit only

Exit status is 1 if anything is found, so it works as a git hook and in CI.

Two deliberate design choices:

1. **Rules are patterns, never literals.** A config file listing "my email is
   X, my username is Y" would itself be the disclosure the moment the repo goes
   public. Everything here matches by *shape* — any email that isn't obviously
   an example, any absolute home path, any credential-shaped assignment.

2. **Matches are redacted in the output.** A scanner that prints the secret it
   found just moves the leak into your CI logs, which are public on a public
   repository.

Stdlib only, so it runs as a git hook with no environment set up.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass, field

# --------------------------------------------------------------------------- #
# Rules
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Rule:
    name: str
    severity: str  # "critical" | "high" | "medium"
    pattern: str
    why: str
    allow: tuple[str, ...] = field(default=())

    def regex(self) -> re.Pattern[str]:
        return re.compile(self.pattern)

    def permitted(self, match: str) -> bool:
        return any(re.search(a, match) for a in self.allow)


# Addresses that are safe by construction: reserved example domains, GitHub's
# no-reply form, and any address whose local part is literally "noreply".
EMAIL_ALLOW = (
    r"@example\.(com|org|net)\b",
    r"@users\.noreply\.github\.com\b",
    r"^noreply@",
    r"@localhost\b",
)

RULES: tuple[Rule, ...] = (
    Rule(
        "private-key",
        "critical",
        r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----",
        "a private key must never be committed",
    ),
    Rule(
        "api-token",
        "critical",
        r"\b(?:sk-ant-[A-Za-z0-9_-]{16,}"
        r"|sk-[A-Za-z0-9]{24,}"
        r"|gh[pousr]_[A-Za-z0-9]{28,}"
        r"|github_pat_[A-Za-z0-9_]{30,}"
        r"|AKIA[0-9A-Z]{16}"
        r"|AIza[0-9A-Za-z_-]{30,}"
        r"|xox[abprs]-[A-Za-z0-9-]{10,})",
        "looks like a live API token or key",
    ),
    Rule(
        "jwt",
        "critical",
        r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}",
        "looks like a signed JWT / bearer token",
    ),
    Rule(
        "credential-assignment",
        "critical",
        r"[\"']?(?:access_?[Tt]oken|refresh_?[Tt]oken|client_?secret"
        r"|api_?key|password|authorization)[\"']?\s*[:=]\s*[\"'][^\"'\s]{16,}[\"']",
        "a credential field with a real-looking value",
        # Placeholders and f-string interpolation are how the source legitimately
        # talks about these fields.
        allow=(r"\{[^}]*\}", r"(?i)(example|placeholder|redacted|xxx+|\.\.\.)"),
    ),
    Rule(
        "email-address",
        "high",
        r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*"
        r"\.[A-Za-z]{2,}",
        "a real email address identifies you",
        allow=EMAIL_ALLOW,
    ),
    Rule(
        "home-path",
        "high",
        r"/(?:home|Users)/(?!runner\b|user\b)[A-Za-z0-9._-]+",
        "an absolute home path reveals your account name",
    ),
    Rule(
        "machine-name",
        "medium",
        r"\b(?:DESKTOP|LAPTOP|MacBook)-[A-Z0-9]{5,}\b",
        "a machine name identifies your device",
    ),
    Rule(
        # Project-specific: screenshots, docs, and fixtures must come from
        # --demo, never a real session. Masking is not anonymisation — the
        # surviving domain suffix still identifies the provider, e.g. a real
        # address leaves ".appleid.com" intact where a placeholder leaves
        # ".com". So allow masks of the reserved example domain and nothing
        # else. Naming that safe shape here discloses nothing.
        "real-account-in-asset",
        "high",
        r"[A-Za-z0-9]•{3}@[A-Za-z0-9]•{3}\.[A-Za-z][A-Za-z0-9.-]*",
        "a masked account from a real session, not the demo data",
        allow=(r"[A-Za-z0-9]•{3}@e•{3}\.com$",),
    ),
)

# Paths whose *content* is allowed to contain rule-shaped text, because their
# whole job is describing these patterns.
PATH_EXEMPT = (
    r"scripts/privacy_scan\.py$",
    r"tests/test_privacy_scan\.py$",
    r"\.github/workflows/privacy\.yml$",
)

# Lock files record upstream URLs and hashes; they are noisy and not authored.
PATH_SKIP = (r"^uv\.lock$", r"(?:^|/)\.git/")


# --------------------------------------------------------------------------- #
# Plumbing
# --------------------------------------------------------------------------- #


@dataclass
class Finding:
    where: str
    line: int | None
    rule: Rule
    sample: str

    def redacted(self) -> str:
        """Never print the secret itself — CI logs are public too."""
        text = self.sample.strip()
        if len(text) <= 8:
            return "*" * len(text)
        return f"{text[:3]}{'*' * min(12, len(text) - 6)}{text[-3:]}"


def run(*args: str) -> str:
    proc = subprocess.run(
        ["git", *args], capture_output=True, text=True, errors="replace"
    )
    return proc.stdout


def exempt(path: str) -> bool:
    return any(re.search(p, path) for p in PATH_EXEMPT)


def skipped(path: str) -> bool:
    return any(re.search(p, path) for p in PATH_SKIP)


def scan_text(text: str, where: str, *, path: str | None = None) -> list[Finding]:
    checked = path if path is not None else where
    if skipped(checked) or exempt(checked):
        return []
    found: list[Finding] = []
    for rule in RULES:
        for match in rule.regex().finditer(text):
            value = match.group(0)
            if rule.permitted(value):
                continue
            line = text.count("\n", 0, match.start()) + 1
            found.append(Finding(where, line, rule, value))
    return found


def scan_worktree() -> list[Finding]:
    found: list[Finding] = []
    for path in run("ls-files").splitlines():
        if not path or skipped(path) or exempt(path):
            continue
        try:
            with open(path, encoding="utf-8") as handle:
                text = handle.read()
        except (OSError, UnicodeDecodeError):
            continue  # binary or unreadable: nothing a regex can find anyway
        found.extend(scan_text(text, path))
    return found


def scan_staged() -> list[Finding]:
    found: list[Finding] = []
    names = run("diff", "--cached", "--name-only", "--diff-filter=ACMR").splitlines()
    for path in names:
        if not path or skipped(path) or exempt(path):
            continue
        blob = run("show", f":{path}")
        found.extend(scan_text(blob, f"{path} (staged)", path=path))
    return found


def scan_history() -> list[Finding]:
    """Content, messages, and identities across every reachable commit.

    History matters as much as the working tree: deleting a secret in a later
    commit leaves it fully readable in the earlier one.
    """
    found: list[Finding] = []
    revs = run("rev-list", "--all").split()
    if not revs:
        return found

    # Identities — the leak that actually happened here once.
    identities = run(
        "log", "--all", "--format=%H%x00%an%x00%ae%x00%cn%x00%ce"
    ).splitlines()
    for entry in identities:
        parts = entry.split("\0")
        if len(parts) != 5:
            continue
        sha, an, ae, cn, ce = parts
        for who, value in (("author", ae), ("committer", ce), ("name", an), ("name", cn)):
            for rule in RULES:
                if rule.name not in ("email-address", "machine-name"):
                    continue
                for match in rule.regex().finditer(value):
                    if rule.permitted(match.group(0)):
                        continue
                    found.append(
                        Finding(f"commit {sha[:9]} ({who})", None, rule, match.group(0))
                    )

    # Messages.
    for sha in revs:
        message = run("log", "-1", "--format=%B", sha)
        for finding in scan_text(message, f"commit {sha[:9]} (message)", path=""):
            found.append(finding)

    # Contents, one git-grep per rule across every commit at once.
    for rule in RULES:
        out = run("grep", "-I", "-n", "-E", rule.pattern, *revs, "--")
        for line in out.splitlines():
            head, _, content = line.partition(":")
            path, _, rest = content.partition(":")
            number, _, body = rest.partition(":")
            if skipped(path) or exempt(path):
                continue
            for match in rule.regex().finditer(body):
                if rule.permitted(match.group(0)):
                    continue
                found.append(
                    Finding(
                        f"{path} @ {head[:9]}",
                        int(number) if number.isdigit() else None,
                        rule,
                        match.group(0),
                    )
                )
    return found


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #

ORDER = {"critical": 0, "high": 1, "medium": 2}


def report(found: list[Finding]) -> int:
    if not found:
        print("privacy scan: clean")
        return 0

    seen: set[tuple[str, int | None, str, str]] = set()
    unique: list[Finding] = []
    for finding in found:
        key = (finding.where, finding.line, finding.rule.name, finding.sample)
        if key not in seen:
            seen.add(key)
            unique.append(finding)
    unique.sort(key=lambda f: (ORDER.get(f.rule.severity, 9), f.where))

    print(f"privacy scan: {len(unique)} finding(s)\n", file=sys.stderr)
    for finding in unique:
        location = finding.where + (f":{finding.line}" if finding.line else "")
        print(
            f"  [{finding.rule.severity.upper()}] {finding.rule.name}\n"
            f"    {location}\n"
            f"    {finding.redacted()}  — {finding.rule.why}",
            file=sys.stderr,
        )
    print(
        "\nNothing was pushed. Fix these, or if a match is a false positive add"
        "\nan allow pattern to the relevant rule in scripts/privacy_scan.py.",
        file=sys.stderr,
    )
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Refuse to publish private data.",
        epilog="With no scope flags, scans the working tree and the full history.",
    )
    parser.add_argument("--staged", action="store_true", help="staged changes only")
    parser.add_argument("--worktree", action="store_true", help="working tree only")
    parser.add_argument("--history", action="store_true", help="every commit only")
    args = parser.parse_args(argv)

    if args.staged:
        return report(scan_staged())
    if args.worktree and not args.history:
        return report(scan_worktree())
    if args.history and not args.worktree:
        return report(scan_history())
    return report(scan_worktree() + scan_history())


if __name__ == "__main__":
    raise SystemExit(main())
