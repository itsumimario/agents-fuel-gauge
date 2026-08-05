<h1 align="center">agents-fuel-gauge</h1>

<p align="center">
  <em>Every subscription quota for Claude Code and Codex, on one screen —<br>
  including the per-model caps that other usage tools quietly drop.</em>
</p>

<p align="center">
  <img alt="Python 3.11+" src="https://img.shields.io/badge/python-3.11%2B-blue">
  <img alt="Platform: Linux" src="https://img.shields.io/badge/platform-Linux-lightgrey">
  <img alt="License: MIT" src="https://img.shields.io/badge/license-MIT-green">
</p>

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/screenshot-dark.png">
    <img alt="agents-fuel-gauge showing Claude and Codex quota bars" src="docs/screenshot-light.png" width="900">
  </picture>
</p>

**`◆` marks the one that runs out first** — the limit that will actually stop
you working. You may have five bars, but only one of them is going to be the
thing that cuts you off, and it isn't always the fullest-looking one.

Each panel shows **its own** last-updated age. The two providers are fetched
concurrently and fail independently, so one can be seconds fresh while the
other is minutes stale. A single global "updated at" would hide exactly that.

> Try it before signing in to anything: **`afg --demo`** renders synthetic data
> with no network access. Every screenshot here is generated from it.

---

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

<details>
<summary><b>The trap, if you're writing your own</b></summary>

The payload *also* contains `seven_day_opus`, `seven_day_sonnet`, and
`seven_day_omelette` (an internal codename). These look like exactly what you
want and are **`null` on current accounts** — they're vestigial. Reading them
is why some trackers show no per-model data at all.

Iterate `limits[]` instead. It's tagged by `kind`
(`session` / `weekly_all` / `weekly_scoped`) and grades itself with `severity`
and `is_active`, so a newly scoped model renders without a code change.

Not every model gets a scoped row — models without a dedicated sub-cap draw
from the shared weekly pool and never appear separately.
</details>

## Requirements

|  |  |
| --- | --- |
| **OS** | Linux (tested on Ubuntu / WSL2) |
| **Python** | 3.11 or newer — the installer provisions this for you |
| **Runtime deps** | [`textual`](https://textual.textualize.io/) ≥ 1.0, [`httpx`](https://www.python-httpx.org/) ≥ 0.27 — installed automatically |
| **Accounts** | Claude Code and/or Codex, already signed in. Either one alone works. |
| **Terminal** | Anything with 256 colours and a monospace font covering `█ ░ ╭ ◆` |

Quota data only exists for **subscription** logins (Claude Pro/Max, ChatGPT
Plus/Pro). API-key authentication has no quota to report and is detected and
explained rather than failing silently.

No sudo. Nothing installed system-wide. No compiler needed.

## Install

```sh
git clone https://github.com/itsumimario/agents-fuel-gauge.git
cd agents-fuel-gauge
./install.sh
```

The installer bootstraps [uv](https://docs.astral.sh/uv/) if you don't have it,
installs into an isolated environment, puts `afg` on your `PATH`, and reports
which agent CLIs you're signed in to.

| | |
| --- | --- |
| `./install.sh` | normal install |
| `./install.sh --editable` | run from the checkout, so edits apply immediately |
| `./install.sh --uninstall` | remove it |

If `~/.local/bin` isn't on your `PATH`, the installer says so and prints the
exact line to add.

<details>
<summary>Manual install, without the script</summary>

```sh
uv tool install .            # or: uv tool install --editable .
```

To hack on it without installing anything globally:

```sh
uv sync && uv run afg
```
</details>

## Usage

```sh
afg                      # live dashboard
afg --check              # one-shot, plain text
afg --check --pretty     # one-shot, with bars
afg --json               # one-shot, machine-readable
afg --demo               # synthetic data, no network
```

### Live dashboard

| Key | Action |
| --- | ------ |
| `r` | refresh now |
| `t` | toggle light/dark |
| `q` | quit |

Refresh interval is floored at **15 seconds** (`-i` to change). The usage
endpoints are themselves rate-limited and will hand you a `429` long before
your real quota runs out — found the hard way while building this.

A failed poll never blanks a panel. The last known bars stay on screen behind a
`⚠ showing last known` warning, and the age keeps counting up, so a transient
`429` or a dropped connection doesn't cost you the numbers you were watching.

### One-shot: `--check`

Prints once and exits, so it works in scripts, cron, shell prompts, and CI.

```console
$ afg --check
claude  5h  all-models           34%  2h10m   -
claude  7d  all-models           68%  18h07m  -
claude  7d  Fable                91%  18h07m  FIRST
codex   7d  all-models           82%  2d04h   FIRST
codex   7d  GPT-5.3-Codex-Spark  12%  6d22h   -
```

Columns are `provider · window · scope · used · resets-in · flags`, aligned and
colour-free. **Every field is a single whitespace-free token**, so awk column
numbers mean what you'd expect:

```sh
afg --check | awk '$4+0 > 80'          # anything over 80% used
afg --check | awk '$6 ~ /FIRST/'       # only what stops you first
```

`FIRST` is the plain-text form of `◆`. `STALE` means the reading was carried
over from an earlier successful poll. **Exits non-zero if any provider failed**,
so `afg --check >/dev/null || notify-send "quota check failed"` behaves.

### One-shot: `--check --pretty`

The same data with bars, for when a human is reading.

```console
$ afg --check --pretty

Claude  (Max 20x · d•••@e•••.com)  just now
    5h all models           ████████░░░░░░░░░░░░░░░░  34%  2h 10m
    7d all models           ████████████████░░░░░░░░  68%  18h 07m
  ◆ 7d Fable                ██████████████████████░░  91%  18h 07m

Codex  (Pro · d•••@e•••.com)  4m ago
  ◆ 7d all models           ████████████████████░░░░  82%  2d 04h
    7d GPT-5.3-Codex-Spark  ███░░░░░░░░░░░░░░░░░░░░░  12%  6d 22h

◆ runs out before the others
```

### JSON output

`afg --json` emits one object per provider. Both vendors are normalised into
the same shape, so consumers never need to know which API a number came from.

```console
$ afg --json
```
```json
[
  {
    "provider": "claude",
    "displayName": "Claude",
    "plan": "Max 20x",
    "account": "d•••@e•••.com",
    "capturedAt": "2026-08-05T23:17:04.470132+00:00",
    "error": null,
    "stale": false,
    "gauges": [
      {
        "window": "5h",
        "scope": "all models",
        "label": "5h all models",
        "percent": 34.0,
        "severity": "normal",
        "runsOutFirst": false,
        "resetsAt": "2026-08-06T01:28:04.470132+00:00",
        "secondsRemaining": 7859
      }
    ]
  }
]
```

| Field | Meaning |
| ----- | ------- |
| `window` | `5h`, `7d`, … — the period the limit covers |
| `scope` | `all models`, or a specific model with its own cap |
| `percent` | 0–100, how much of that window is used |
| `severity` | `normal` / `warning` / `critical` |
| `runsOutFirst` | this is the limit that stops you before the others |
| `secondsRemaining` | until reset, pre-computed so you don't parse dates |
| `stale` | numbers carried over from an earlier poll after a failure |
| `error` | non-null if this provider couldn't be read |

Handy one-liners:

```sh
afg --json | jq -r '.[].gauges[] | select(.runsOutFirst) | "\(.label) \(.percent)%"'
afg --json | jq '[.[].gauges[] | select(.severity=="critical")] | length'
```

## Connecting your accounts

**There's no login step, and this tool never asks for a password or a token.**

It reads the credentials the official CLIs have already written to disk. If
`claude` and `codex` work in your terminal, `afg` works.

| Provider | Read from | Endpoint |
| -------- | --------- | -------- |
| Claude | `~/.claude/.credentials.json` | `api.anthropic.com/api/oauth/usage` |
| Codex | `~/.codex/auth.json` | `chatgpt.com/backend-api/wham/usage` |

Not signed in? Run `claude` or `codex` once, sign in normally, then re-run
`afg`. Each provider is independent — one is enough to get a panel.

<details>
<summary><b>Why piggyback instead of implementing OAuth?</b></summary>

A proper OAuth flow would mean registering this app as a client with Anthropic
and OpenAI and asking you to authorize it. Both usage endpoints are internal
and undocumented, and neither vendor publishes a client-registration path for
third-party quota readers, so there's no legitimate OAuth client to be.

Piggybacking is also better for you:

- **No new secrets.** Nothing is created, stored, or transmitted that doesn't
  already exist on your machine.
- **No separate expiry.** The official CLIs refresh their own tokens in place;
  this tool re-reads the file on every poll and inherits the fresh token for
  free. A cached bearer would strand the dashboard after about eight hours.
- **Nothing to revoke.** Removing the tool leaves no authorization behind.

The cost is a dependency on file locations the vendors could change. The
override variables below are the escape hatch if that happens.
</details>

<details>
<summary>Non-standard credential locations</summary>

The vendors' own variables are respected first:

| Variable | Effect |
| -------- | ------ |
| `CLAUDE_CONFIG_DIR` | Claude Code's own config-directory override |
| `CODEX_HOME` | Codex's own home override |
| `AFG_CLAUDE_CREDENTIALS` | full path to the credentials file |
| `AFG_CODEX_AUTH` | full path to the auth file |
</details>

### Privacy

Two read-only `GET`s to the vendors' own usage endpoints, using tokens already
on your machine. Nothing is written, cached to disk, or sent anywhere else —
no telemetry, no server, no third party in the path.

Account emails are masked (`d•••@e•••.com`) and file paths render as `~/…`, so
screenshots and pasted output stay shareable.

## Provider quirks

- **Codex has no `severity` field**, so it's derived from thresholds
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
uv sync
uv run pytest
uv run python scripts/screenshot.py    # regenerate README images from demo data
```

Tests are built from real payload shapes and pin the two things most likely to
rot: that scoped per-model limits are read out of `limits[]`, and that the dead
`seven_day_*` keys never produce phantom bars. The UI tests drive the real
Textual app headless.

Screenshots are always generated from `--demo`, so no real account usage can
end up in the repository.

## License

[MIT](LICENSE)
