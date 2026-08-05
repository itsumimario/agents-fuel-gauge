# agents-fuel-gauge

A small terminal dashboard showing **every** subscription quota window for
Claude Code and Codex on one screen — including the per-model caps that most
usage tools quietly drop.

```
   runs out first: Claude 7d Fable  91% used  ·  resets in 17h 38m

 ╭─ Claude ──────────────────── Max 20x · a•••@e•••.com · 12s ago ─╮
 │    5h all models      ███░░░░░░░░░░░░░░░░░░░░░░░░░    6%  48m   │
 │    7d all models      ███████████████░░░░░░░░░░░░░   50%  17h   │
 │  ◆ 7d Fable           ██████████████████████████░░   91%  17h   │
 ╰─────────────────────────────────────────────────────────────────╯
 ╭─ Codex ───────────────────────────── Pro · b•••@e•••.com · 12s ago ─╮
 │  ◆ 7d all models      █████████████████████████████  98%  2d 05h │
 │    7d GPT-5.3-Codex-Spark ░░░░░░░░░░░░░░░░░░░░░░░░░   0%  6d 23h │
 ╰─────────────────────────────────────────────────────────────────╯
```

**`◆` marks the one that runs out first** — the limit that will actually stop
you working. You may have five bars, but only one of them is going to be the
thing that cuts you off, and it is not always the fullest-looking one.

Each panel shows **its own** last-updated age. The two providers are fetched
concurrently and fail independently, so one can be seconds fresh while the
other is minutes stale — a single global "updated at" would hide exactly that.

## Why this exists

Most usage tools report two numbers for Claude: the 5-hour session window and
the aggregate 7-day window. On a plan where a specific model carries its own
weekly sub-cap, that aggregate is actively misleading — it can read a
comfortable **50%** while the model-specific cap sits at **91%** and the API has
already graded it `critical`.

The data was there the whole time. `GET /api/oauth/usage` returns a
self-describing `limits[]` array carrying every window, including scoped
per-model entries:

```json
{ "kind": "weekly_scoped", "percent": 91, "severity": "critical",
  "scope": { "model": { "display_name": "Fable" } }, "is_active": true }
```

Tools miss it because they read the two flat top-level keys (`five_hour`,
`seven_day`) and never touch `limits[]`.

### The trap

The payload *also* contains `seven_day_opus`, `seven_day_sonnet`, and
`seven_day_omelette` (an internal codename). These look like exactly what you
want and are **`null` on current accounts** — they are vestigial. Reading them
is why some trackers show no per-model data at all.

Iterate `limits[]` instead. It is tagged by `kind`
(`session` / `weekly_all` / `weekly_scoped`) and grades itself with `severity`
and `is_active`, so a newly scoped model renders without a code change.

Note that not every model gets a scoped row — models without a dedicated
sub-cap simply draw from the shared weekly pool and never appear separately.

## Install

Linux, no sudo, nothing system-wide:

```sh
git clone https://github.com/itsumimario/agents-fuel-gauge.git
cd agents-fuel-gauge
./install.sh
```

The installer bootstraps [uv](https://docs.astral.sh/uv/) if you don't have it,
installs into an isolated environment, puts `afg` on your PATH, and tells you
whether you're signed in to each CLI.

| | |
| --- | --- |
| `./install.sh` | normal install |
| `./install.sh --editable` | run from the checkout, so edits apply immediately |
| `./install.sh --uninstall` | remove it |

If `~/.local/bin` isn't on your PATH the installer says so and prints the line
to add.

<details>
<summary>Manual install</summary>

```sh
uv tool install .          # or: uv tool install --editable .
```

To hack on it without installing anything: `uv sync && uv run afg`.
</details>

## Usage

```sh
afg                      # live dashboard
afg --check              # one-shot, plain text (pipeable, no colour)
afg --check --pretty     # one-shot, with bars
afg --json               # one-shot, machine-readable
```

`--check` is the spot check: it prints once and exits, so it works in scripts,
cron, prompts, and pull-request comments.

```
$ afg --check
claude  5h  all models           6%  48m 04s   -
claude  7d  all models          50%  17h 38m   -
claude  7d  Fable               91%  17h 38m   FIRST
codex   7d  all models          98%  2d 05h    FIRST
codex   7d  GPT-5.3-Codex-Spark  0%  6d 23h    -
```

Columns are `provider · window · scope · used · resets-in · flags`, tab-aligned
and colour-free so `awk '$4+0 > 80'` does what you'd expect. `FIRST` is the
plain-text form of `◆`; `STALE` means the reading was carried over from an
earlier successful poll.

`--check` and `--json` exit non-zero if any provider failed.

### In the dashboard

| Key | Action |
| --- | ------ |
| `r` | refresh now |
| `t` | toggle light/dark |
| `q` | quit |

Refresh interval is floored at **15s** (`-i` to change). The usage endpoints are
themselves rate-limited and will hand you a `429` long before your real quota
runs out — found the hard way while building this.

A failed poll never blanks a panel. The last known bars stay on screen with a
`⚠ showing last known` warning and the age keeps counting up, so a transient
`429` or a dropped connection doesn't cost you the numbers you were watching.

## Connecting your accounts

**There is no login step, and this tool never asks for a password or token.**

It piggybacks on the credentials the official CLIs have already written to
disk. If `claude` and `codex` work in your terminal, `afg` works.

| Provider | Read from | Endpoint |
| -------- | --------- | -------- |
| Claude | `~/.claude/.credentials.json` | `api.anthropic.com/api/oauth/usage` |
| Codex | `~/.codex/auth.json` | `chatgpt.com/backend-api/wham/usage` |

Not signed in? Run `claude` or `codex` once, sign in normally, and re-run `afg`.
Each provider is independent — one signed-in CLI is enough to get a panel.

<details>
<summary>Why piggyback instead of implementing OAuth?</summary>

A proper OAuth flow would mean registering this app as a client with Anthropic
and OpenAI and asking you to authorize it. Both usage endpoints are internal
and undocumented, and neither vendor publishes a client registration path for
third-party quota readers, so there is no legitimate OAuth client to be.

Piggybacking is also better for you:

- **No new secrets.** Nothing is created, stored, or transmitted that doesn't
  already exist on your machine.
- **No separate expiry.** The official CLIs refresh their own tokens in place;
  this tool re-reads the file on every poll and inherits the fresh token for
  free. A cached bearer would strand the dashboard after about eight hours.
- **Nothing to revoke.** Deleting the tool leaves no authorization behind.

The cost is that it depends on file locations the vendors could change. If that
happens, the override env vars below are the escape hatch.
</details>

<details>
<summary>Non-standard credential locations</summary>

The official env vars are respected first:

| Variable | Effect |
| -------- | ------ |
| `CLAUDE_CONFIG_DIR` | Claude Code's own config dir override |
| `CODEX_HOME` | Codex's own home override |
| `AFG_CLAUDE_CREDENTIALS` | full path to the credentials file |
| `AFG_CODEX_AUTH` | full path to the auth file |

</details>

### What it sends, and where

Two read-only GETs to the vendors' own usage endpoints, using the tokens
already on your machine. Nothing is written, cached to disk, or sent anywhere
else — there is no telemetry, no server, and no third party in the path.

Account emails are masked (`a•••@e•••.com`) and file paths are shown as
`~/...` so screenshots and pasted output stay shareable.

## Provider quirks

- **Codex has no `severity` field**, so it is derived from thresholds
  (≥60% warning, ≥85% critical). It also doesn't flag which limit binds, so the
  fullest bar gets `◆` by inference. Claude reports both directly.
- **Codex windows are identified by duration, not name** —
  `limit_window_seconds: 604800` is weekly, `18000` is the 5-hour window. On
  plans with no 5-hour throttle, `secondary_window` is `null` and no bar is
  drawn.
- **Codex scoped limits** live in `additional_rate_limits[]`, the rough
  equivalent of Anthropic's `weekly_scoped` entries.

## Development

```sh
uv sync && uv run pytest
```

Tests are built from real payload shapes and pin the two things most likely to
rot: that scoped per-model limits are read out of `limits[]`, and that the dead
`seven_day_*` keys never produce phantom bars. The UI tests drive the real
Textual app headless.

## Status

A deliberate one-off, Linux-only for now. The fetch-and-normalize half
(`sources.py` → `models.py`) is written to be lifted out later if it's ever
worth generalizing — `afg --json` is already that seam.
