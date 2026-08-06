<h1 align="center">agents-fuel-gauge</h1>

<p align="center">
  <em>All your Claude Code and Codex usage limits, on one screen.</em>
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

<p align="center">
  <sub>Terminal interface built with <a href="https://textual.textualize.io/">Textual</a>.</sub>
</p>

## No sign-in. No API keys. No config.

> **If `claude` or `codex` already works in your terminal, this already works.**
>
> It reads the credentials those CLIs have already written to your machine.
> There is nothing to authorize, nothing to paste, and nothing to set up.
> Install it and run `afg`.

Either CLI alone is enough — you'll just get one panel instead of two.

## Install

```sh
curl -LsSf https://raw.githubusercontent.com/itsumimario/agents-fuel-gauge/main/install.sh | bash
```

That's it. It brings its own Python toolchain, installs into an isolated
environment, and puts `afg` on your `PATH`. No sudo, nothing system-wide.

Then:

```sh
afg
```

<details>
<summary>Other ways to install</summary>

```sh
git clone https://github.com/itsumimario/agents-fuel-gauge.git
cd agents-fuel-gauge
./install.sh                 # normal
./install.sh --editable      # run from the checkout; edits apply immediately
./install.sh --uninstall     # remove it
```

Or with [uv](https://docs.astral.sh/uv/) directly:

```sh
uv tool install git+https://github.com/itsumimario/agents-fuel-gauge.git
```
</details>

## Why this exists

- **One screen for both providers.** Claude and Codex quotas live in two
  different places and neither shows the other. This shows all of it, live.
- **It shows the limits other tools hide** — including per-model weekly caps,
  which can sit at 91% while the headline number still reads a comfortable 50%.
- **It tells you how much to slow down or speed up**, per meter, not just how
  full the bar is. 91% used is fine with an hour left and a crisis with four
  days left.
- **One-shot from the command line.** `afg --check` prints once and exits, for
  scripts, prompts, cron, and CI.
- **A tiny JSON service for anything else.** `afg --json` gives other tools a
  clean, normalised feed — and a rate instruction per meter they can act on.

## Usage

```sh
afg                      # live dashboard
afg --check              # one-shot, plain text
afg --check --pretty     # one-shot, with bars
afg --json               # one-shot, machine-readable
afg --watch --json       # keep emitting, one JSON object per line
afg --demo               # synthetic data, no accounts needed
afg --update             # update to the latest version
```

### The arrow tells you what to do

Every row ends in an instruction, not a status report. The arrow points the way
you should move, and says how far:

| | |
| --- | --- |
| `↓ by 92%` | **slow down.** At this rate the meter runs out before it resets — cut to 8% of it |
| `↑ by 150%` | **speed up.** There is room for two and a half times this rate |
| `·` | on budget — this rate lasts exactly to the reset |
| `✗` | spent |
| `◦` | window too new to judge |

The percentage is the *change*, so `↓ by 92%` and "throttle to 8%" are the same
instruction — the first is just readable at a glance. It's measured against
each meter's **average rate so far**, since one reading is all a snapshot gives
you. A `+` (`↑ by 900%+`) means the figure hit its cap and is a floor, not a
measurement.

That number is what makes the whole thing actionable: a bar at 91% tells you
to panic or relax depending on facts that aren't on the screen. `↓ by 92%`
tells you what to actually change.

Each panel shows its own last-updated age, because the two providers are
fetched independently and one can go stale while the other keeps refreshing.

### Live dashboard

| Key | Action |
| --- | ------ |
| `r` | refresh now |
| `t` | toggle light/dark |
| `q` | quit |

Refreshes every 60s (`-i` to change, 15s minimum). A failed refresh never
blanks a panel — the last known numbers stay up behind a warning.

### One-shot: `--check`

```console
$ afg --check
claude  5h  all-models           34%  2h10m   -       spare_capacity  2026-08-06T10:31  2h10m   +150%
claude  7d  all-models           68%  18h07m  -       spare_capacity  2026-08-07T02:28  18h07m  +289%
claude  7d  Fable                91%  18h07m  ACTIVE  on_track        2026-08-07T02:28  18h07m  -
codex   7d  all-models           82%  5d01h   -       slow_down       2026-08-11T10:20  5d1h    -92%
codex   7d  GPT-5.3-Codex-Spark  12%  6d21h   -       too_early       2026-08-13T06:20  6d21h   -
```

| | Column | |
| --- | --- | --- |
| `$1` | provider | |
| `$2` | window | `5h`, `7d` |
| `$3` | scope | `all-models` or a model name |
| `$4` | used | percent |
| `$5` | resets in | countdown, to the second in the last hour |
| `$6` | flags | `ACTIVE` — the provider says this limit is the one in force. `STALE` — carried over from an earlier poll |
| `$7` | pace verdict | |
| `$8` | resets at | local time, ISO-ish |
| `$9` | remaining | `18h07m` inside a day, `5d1h` past one |
| `$10` | change | signed: `-92%` means slow down by 92% |

Every field is a single whitespace-free token, so awk columns mean what you'd
expect:

```sh
afg --check | awk '$4+0 > 80'           # anything over 80% used
afg --check | awk '$6 ~ /ACTIVE/'       # only the limit currently in force
afg --check | awk '$10+0 < -50'         # only what needs real throttling
```

Note the difference between columns 4 and 7 in that output: Fable at **91%** is
`on_track` because the week is nearly over, while Codex at **82%** is
`slow_down` because five days remain. The percentage alone would have ranked
them the other way round.

Columns are only ever appended, never inserted, so scripts written against an
older version keep working.

Exits non-zero if a provider couldn't be read.

Add `--pretty` for the same data with bars, when a human is reading.

### JSON

`afg --json` emits an envelope: **one directive per meter**, plus the full
per-provider detail if you want to look closer. Both providers are normalised
into the same shape, so consumers never need to know which API a number came
from.

```json
{
  "at": "2026-08-06T12:11:46.572952+00:00",
  "directives": [
    {
      "provider": "codex",
      "label": "7d all models",
      "scope": "all models",
      "window": "7d",
      "percent": 82.0,
      "severity": "warning",
      "verdict": "slow_down",
      "actionable": true,
      "direction": "down",
      "rateAdjustment": 0.078,
      "changePercent": -92.2,
      "advice": "slow this meter down by 92% — to 8% of its average rate",
      "projectedUsagePercent": 287.0,
      "resetsAt": "2026-08-11T10:05:46+00:00",
      "secondsRemaining": 435239
    }
  ],
  "providers": [ … ]
}
```

#### The directives

| Field | Meaning |
| ----- | ------- |
| `provider`, `label` | which meter this applies to — **only** that meter |
| `verdict` | `slow_down` / `on_track` / `spare_capacity` / `exhausted` / `too_early` |
| `direction` | `down` / `up` / `hold` — the arrow, as a value |
| `changePercent` | signed change to make, or `null` when no change is called for. `-92.2` = ease off by 92% |
| `rateAdjustment` | the same instruction as a multiplier, for code that scales a rate directly. `0.078` = throttle to 7.8% |
| `actionable` | `false` when the window is too new to judge |
| `projectedUsagePercent` | where you land at reset if nothing changes. Over 100 means you run out early |

**There is deliberately no single combined instruction.** An earlier version
ranked every meter together and emitted one winner with one multiplier. That
was wrong twice over: ranking implies a prediction the data can't support —
`used / elapsed` is an *average since the window opened*, so a meter burned
hard days ago and idle since looks identical to one burning steadily now — and
a lone multiplier reads as advice about your whole workload when it only ever
described one window.

A window that has only just opened reports `too_early`, `actionable: false` and
`rateAdjustment: 1.0` — a safe no-op. Minutes of data divide into a wild ratio,
and throttling on that would be worse than doing nothing.

#### Per-gauge detail

Each gauge under `providers[].gauges[]` carries `window`, `scope`, `label`,
`percent`, `severity`, `activeLimit`, `resetsAt`, `secondsRemaining`,
`windowSeconds`, and its own `pace` object.

```sh
afg --json | jq -r '.directives[] | select(.direction == "down")
                    | "\(.provider) \(.label): \(.changePercent)%"'
afg --json | jq -r '.providers[].gauges[] | select(.activeLimit)
                    | "\(.label) \(.percent)%"'
```

### Subscribing

`afg --watch --json` keeps sampling and emits **one JSON object per line**,
flushed immediately — so anything that can read a pipe can subscribe:

```sh
afg --watch --json -i 60 | while read -r line; do
  jq -r '.directives[] | select(.verdict == "slow_down")
         | "throttle \(.provider)/\(.label) to \(.rateAdjustment)x"' <<<"$line"
done
```

That's the whole mechanism: no daemon, no socket, no broker.

### Rate limiting

The vendor usage endpoints are themselves rate-limited, and polling them hard
earns a 429 long before your real quota runs out. So readings are cached on
disk and shared between every `afg` process: a status bar polling once a second
still costs one request a minute. A 429 is recorded with its `Retry-After`, and
nothing is attempted until it expires — retrying into a closed door is what
turns one 429 into a stream of them.

```sh
afg --max-age 300     # reuse a reading up to 5 minutes old
afg --no-cache        # always call the API (can earn you a 429)
afg --clear-cache     # drop cached readings and any standing backoff
```

## Updating

```sh
afg --update
```

Pulls the latest version and reinstalls. It refuses to touch a checkout with
uncommitted changes.

## Requirements

|  |  |
| --- | --- |
| **OS** | Linux |
| **Python** | 3.11+ — the installer provisions it for you |
| **Accounts** | Claude Code and/or Codex, already signed in |
| **Terminal** | 256 colours, a monospace font covering `█ ░ ╭ ↑ ↓` |

Quota data exists only for **subscription** logins (Claude Pro/Max, ChatGPT
Plus/Pro). API-key auth has no quota to report; that's detected and explained
rather than failing silently.

<details>
<summary>Non-standard credential locations</summary>

| Variable | Effect |
| -------- | ------ |
| `CLAUDE_CONFIG_DIR` | Claude Code's own config-directory override |
| `CODEX_HOME` | Codex's own home override |
| `AFG_CLAUDE_CREDENTIALS` | full path to the credentials file |
| `AFG_CODEX_AUTH` | full path to the auth file |
</details>

### What it sends, and where

Two read-only `GET`s to the providers' own usage endpoints, using tokens
already on your machine:

| Provider | Endpoint |
| -------- | -------- |
| Claude | `api.anthropic.com/api/oauth/usage` |
| Codex | `chatgpt.com/backend-api/wham/usage` |

Nothing is sent anywhere else. No telemetry, no server, no third party. The
only thing written to disk is the response cache described above, under your
XDG cache directory. Account emails are masked and paths render as `~/…`, so
screenshots stay shareable.

## Notes

- **Codex doesn't grade its own limits**, so severity is derived from
  thresholds (≥60% warning, ≥85% critical). Claude reports its own. Neither
  gets an "in force" flag invented for it: Anthropic reports one, OpenAI
  doesn't, so no Codex bar claims to be the binding one.
- **Codex identifies windows by duration, not name.** On plans with no 5-hour
  throttle, no 5h bar is drawn because the API doesn't return one.
- **Not every model gets its own bar** — only those with a dedicated sub-cap.
  The rest draw from the shared pool.

## Development

```sh
uv sync
uv run pytest
uv run python scripts/screenshot.py    # regenerate README images from demo data
```

Screenshots are always generated from `--demo`, so no real account data can
end up in the repository.

## Credits

**agents-fuel-gauge** by **ItsumiMario**.

Built with [Textual](https://textual.textualize.io/) for the terminal UI,
[httpx](https://www.python-httpx.org/) for the HTTP client, and
[uv](https://docs.astral.sh/uv/) for packaging and installation.

## License

[MIT](LICENSE) © ItsumiMario
