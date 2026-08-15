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

Either CLI alone is enough. The live dashboard only draws panels for CLIs that
are installed, so a Codex-only setup gets one useful panel rather than a second
box explaining Claude. Installed-but-unavailable CLIs stay visible with their
error, because a sign-in or network problem is something you can act on.

## Install

```sh
curl -LsSf https://raw.githubusercontent.com/itsumimario/agents-fuel-gauge/main/install.sh | bash
```

That's it. It brings its own Python toolchain, installs into an isolated
environment, installs AFG's declared runtime dependencies (including Textual
and plotext), and puts `afg` on your `PATH`. No pnpm, sudo, separate dependency
installs, or system-wide packages are required.

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
- **History makes a course correction visible.** Press `h` to compare the
  actual trace with the on-budget diagonal and the rate required from here.
  Rate chunks summarize stretches of steady percent-tick movement, so a new
  pace stays visible beside the old one. Only the latest chunk gets an arrow:
  older chunks are context, not behavior you can still change. The default
  view zooms to the interval actually recorded; `z` restores the complete quota
  window, while `d` expands the inferred chunks into individually scaled plots.
- **One-shot from the command line.** `afg --check` prints once and exits, for
  scripts, prompts, cron, and CI.
- **A tiny JSON service for anything else.** `afg --json` gives other tools a
  clean, normalised feed — and a rate instruction per meter they can act on.
- **Placement advice for Stellate leaders.** `afg --recommend-minion` balances
  current Sol and Opus 5 quota pressure and emits one stable choice a leader
  can use before creating a minion.

## Usage

```sh
afg                      # live dashboard
afg --check              # one-shot, plain text
afg --check --pretty     # one-shot, with bars
afg --json               # one-shot, machine-readable
afg --watch --json       # keep emitting, one JSON object per line
afg --recommend-minion   # emit sol or opus-5 for a new Stellate minion
afg --demo               # synthetic data, no accounts needed
afg --update             # update to the latest version
```

### Choosing a provider for a Stellate minion

Ask AFG immediately before creating a minion:

```sh
agent=$(afg --recommend-minion)       # stdout is exactly: sol or opus-5
afg --recommend-minion --duration 2h --effort high
```

A successful plain query prints exactly one supported recommendation:
`sol` means Codex with `gpt-5.6-sol`; `opus-5` means Claude with `opus-5`.
Warnings go to stderr, so command substitution remains safe. AFG exits nonzero
without emitting either value when neither candidate has usable, available
quota data. It never guesses through missing data or recommends an already
exhausted candidate.

Those values are AFG's stable policy vocabulary, not necessarily identifiers a
vendor CLI accepts. JSON consumers should launch with `vendor` and `cli_model`:
for example, the Opus recommendation carries `vendor: "claude"` and
`cli_model: "claude-opus-5"`. The separate `model` field remains AFG's
canonical `opus-5`; do not pass it verbatim to the Claude CLI.

The optional inputs describe the proposed work:

| Option | Effect |
| --- | --- |
| `--duration 45m` | Discounts pressure from a quota window that resets during the expected run. Accepts `s`, `m`, `h`, `d`, and `w`, including forms such as `1h30m`. |
| `--effort low` | Keeps the Sol preference unless Opus 5 is more than 20 quota-pressure points healthier. |
| `--effort medium` | The default; switches when Opus 5 is more than 10 points healthier. |
| `--effort high` | Switches when Opus 5 is more than 5 points healthier, reserving the preferred provider less aggressively for substantial work. |

AFG scores each candidate by its tightest applicable quota meter. It considers
both the percentage already used and the usage projected at reset from the
window's average pace. All-model meters always apply; a matching model-scoped
meter also applies, as does a Claude scoped meter the provider explicitly marks
active. Near ties go to Sol. If only one candidate has usable data and quota,
that candidate wins.

This is a quota-pressure heuristic, not a token forecast. AFG does not know the
task's future consumption or pretend that one percentage point represents the
same number of tokens on two different subscriptions. Duration only says how
long current-window pressure remains relevant; effort controls how large the
health advantage must be to override the Sol preference.

Use `--json` for the evidence behind the choice:

```sh
afg --recommend-minion --duration 2h --effort high --json
```

```json
{
  "schema": "afg.minion-recommendation/v1",
  "recommendation": "sol",
  "vendor": "codex",
  "model": "gpt-5.6-sol",
  "cli_model": "gpt-5.6-sol",
  "effort": "high",
  "expectedDurationSeconds": 7200.0,
  "method": "worst reset-adjusted quota pressure",
  "reason": "Sol is preferred because Opus 5 is not more than 5.0 quota-pressure points healthier",
  "stale": false,
  "warnings": [],
  "candidates": {
    "sol": {
      "cli_model": "gpt-5.6-sol",
      "status": "ready",
      "pressure": 73.9,
      "meters": [{ "label": "7d all models", "pressure": 73.9 }]
    },
    "opus-5": {
      "cli_model": "claude-opus-5",
      "status": "ready",
      "pressure": 127.5,
      "meters": [{ "label": "7d Fable", "pressure": 127.5 }]
    }
  }
}
```

The normal five-minute shared cache applies. If a fresh poll fails but cached
gauges survive, AFG may still recommend from them: plain mode warns on stderr,
while JSON sets `stale: true`, preserves each candidate's `capturedAt` and
`dataAgeSeconds`, and includes the warning. A JSON failure uses the same schema
with `error.code: "no_usable_candidate"` and exits nonzero. Candidate records
in both success and failure payloads include `cli_model`, so a consumer can
inspect either vendor without maintaining its own model-name translation.

### The arrow tells you what to do

When AFG has safe advice, the arrow points the way you should move and says how
far. Otherwise the row uses words to report status without inventing a move:

| | |
| --- | --- |
| `↓ by 92%` | **slow down.** At this rate the meter runs out before it resets — cut to 8% of it |
| `↑ by 150%` | **speed up.** There is room for two and a half times this rate |
| `↑ headroom` | more usage is supportable, but the increase is too large to estimate reliably |
| `on pace` | this rate lasts exactly to the reset |
| `✗ spent` | nothing left until it resets |
| `too new` | the window has barely opened; no rate worth estimating yet |

An arrow only appears where there's a direction to point. The rest are words,
because a glyph you have to look up is a question, not information — and the
`·` and `◦` this replaced were a pixel apart while meaning opposite things
about whether the reading could be trusted.

**Only safe, jointly compatible advice gets an arrow.** Meters aren't
independent: one request spends from all of them at once, so a 5-hour window
with headroom to spare cannot license spending that a weekly window forbids. A
weekly window that is still `too new` cannot prove you should slow down, but it
does veto “speed up” until enough of that window has elapsed. Its `too new`
status remains visible even when another meter governs.

```
5h all models  ████░░  18%  Sat Aug  8 14:51   1h 01m
7d all models  █████░  88%  Mon Aug 10 13:57    2d 0h  ↓ by 66%
7d Fable       █████░  91%  Mon Aug 10 13:57    2d 0h  ↓ by 75%
```

The 5-hour meter has plenty left and says nothing, because none of it is
yours to spend. Scope decides what a meter can constrain: `all models` governs
everything, while a per-model cap like Fable governs only that model — which is
why it can add a second, tighter instruction without throttling anything else.

The percentage is the *change*, so `↓ by 92%` and "throttle to 8%" are the same
instruction — the first is just readable at a glance. It's measured against
each meter's **average rate so far**, since one reading is all a snapshot gives
you. If the increase calculation hits AFG's 10× safety cap, the row says
`↑ headroom` instead of printing a fake `900%` recommendation.

An evidence-backed number is what makes the whole thing actionable: a bar at
91% tells you to panic or relax depending on facts that aren't on the screen.
`↓ by 92%` tells you what to actually change; `headroom` is deliberately less
specific when the arithmetic cannot support a magnitude.

Each panel shows its own last-updated age, because the two providers are
fetched independently and one can go stale while the other keeps refreshing.
The masked account remains visible after the first refresh; at phone widths the
plan label yields first and the age becomes compact before the account yields.

### Live dashboard

| Key | Action |
| --- | ------ |
| `r` | refresh now |
| `t` | switch to `Light` or `Dark` (the button names the destination) |
| `h` | open `History`, then return to `Gauges` |
| `z` | open `Full range`, then return to `Recorded range` (Overview only) |
| `m` | choose the `Next meter` recorded for each provider (History only) |
| `d` | open `Segments`, then return to `Overview` (History only) |
| `o` | open `Options` |
| `q` | quit |

The footer always names the destination of a key press. In History, the panel
border and subtitle name the currently selected provider, meter, range, and
layout.

Refreshes every five minutes (`-i` to change, 15s minimum); ages and reset
countdowns still tick locally every second. `r` is an explicit live probe: it
bypasses both AFG's five-minute response cache and a persisted backoff that may
have become obsolete. If the provider still returns 429, AFG keeps the last
known numbers and shows which PID triggered it, whether it was an automatic
poll or manual refresh, and when automatic polling will retry.

At phone widths the clickable key buttons wrap onto a second row instead of
scrolling off-screen.

#### History at a glance

- **History / Gauges (`h`) changes the screen.** History replaces the live
  quota bars with graphs; Gauges returns to the bars.
- **Next meter (`m`) changes the data series.** It advances each provider from
  its selected quota to the next one with drawable samples: for example,
  Claude 5h all models → Claude 7d all models → Claude 7d Fable. Every
  graph border names its selected provider and meter, and the selection
  survives refreshes and view changes.
- **Full range / Recorded range (`z`) changes the framing.** Recorded range
  enlarges the observed part of the selected meter. Full range restores that
  meter's entire quota window from 0–100%. The data itself does not change.
- **Segments / Overview (`d`) changes the layout.** Overview shows one graph
  for the selected meter. Segments slices the same trace at transitions between
  sustained linear and variable behavior and shows up to five resulting
  portions as separate, newest-first graphs.

The solid line is observed usage; the gray diagonal is an even budget pace;
the dotted line is the pace needed from the latest sample to reach 100% exactly
at reset. The rate readout beneath an Overview graph summarizes changes in how
quickly usage was rising. Only its latest rate gets an advice arrow, because
older rates are context rather than behavior you can change.

#### Recorded range and Full range

History opens in the phone-friendly **Recorded range** around samples that
actually exist, with a little padding and the percentage scale fitted to the
visible lines. This avoids spending most of the graph on either unrecorded days
before the trace or future days that have not happened. Press `z` for the
0–100%, whole-window **Full range** and again to return to Recorded range.

<table>
  <tr>
    <th>Recorded range — the useful observed interval</th>
    <th>Full range — the complete quota window</th>
  </tr>
  <tr>
    <td><img alt="Recorded range tightly framing the synthetic Claude usage trace" src="docs/history-recorded-dark.png"></td>
    <td><img alt="Full range showing the same synthetic Claude trace in its complete quota window" src="docs/history-full-dark.png"></td>
  </tr>
</table>

#### Segments

Press `d` when the overview compresses a meaningful pace change too tightly.
AFG treats sustained straight portions as delimiters and keeps the changing
shape between them intact. The resulting linear and variable portions become
independently scaled, vertically stacked plots, so a brief curve remains
readable on a narrow screen instead of being flattened into an average. Linear
portions show a fitted rate and gray fit line; variable portions show only the
observed shape and label its end-to-end rate as an average. If there is not
enough history to classify the shape yet, AFG says so instead of inventing a
portion. In Segments, `z` is hidden because the portion plots already own their
viewports.

<p align="center">
  <img alt="Segments history view showing five newest-first linear and variable portions of the synthetic Claude trace" src="docs/history-details-dark.png" width="900">
</p>

<p align="center"><sub>Recorded range, Full range, and Segments all show the same deterministic synthetic Claude trace; no account history is read.</sub></p>

The graph legend decodes the solid usage trace (green normal, orange warning,
red critical), the dim gray ideal-budget line, and the green dotted path needed
to reach 100% at reset. In Segments, a gray line instead marks the fit for a
linear portion; variable portions deliberately have no straight fit line.
Pace advice continues to use the whole-window average, so changing the chart
range or mode changes no directive.

### One-shot: `--check`

```console
$ afg --check
claude  5h  all-models           18%  1h01m  -               spare_capacity  2026-08-08T14:52  1h01m  -
claude  7d  all-models           88%  2d00h  GOVERNS         slow_down       2026-08-10T13:58  2d0h   -66%
claude  7d  Fable                91%  2d00h  ACTIVE,GOVERNS  slow_down       2026-08-10T13:58  2d0h   -75%
codex   7d  all-models           62%  1d02h  -               spare_capacity  2026-08-09T16:50  1d2h   +220%
codex   7d  GPT-5.3-Codex-Spark  12%  6d21h  -               too_early       2026-08-15T11:50  6d21h  -
```

| | Column | |
| --- | --- | --- |
| `$1` | provider | |
| `$2` | window | `5h`, `7d` |
| `$3` | scope | `all-models` or a model name |
| `$4` | used | percent |
| `$5` | resets in | countdown, to the second in the last hour |
| `$6` | flags | `GOVERNS` — this meter is the tightest constraint on the work it covers, so its advice is actionable. `ACTIVE` — the provider says this limit is the one in force. `STALE` — carried over from an earlier poll |
| `$7` | pace verdict | this meter's own reading |
| `$8` | resets at | local time, ISO-ish |
| `$9` | remaining | `18h07m` inside a day, `5d1h` past one |
| `$10` | change | signed: `-92%` means slow down by 92% |

Columns 7 and 10 are the meter's *own* reading; `GOVERNS` is what makes that
reading safe to act on. A capped increase has no numeric change, and a meter
with apparent headroom does not gain `GOVERNS` while an overlapping window is
`too_early`.

Every field is a single whitespace-free token, so awk columns mean what you'd
expect:

```sh
afg --check | awk '$4+0 > 80'                       # anything over 80% used
afg --check | awk '$6 ~ /GOVERNS/'                  # only what constrains you
afg --check | awk '$6 ~ /GOVERNS/ && $10+0 < -50'   # only real throttling
```

Note the difference between columns 4 and 7 in that output: Fable at **91%** is
`on_track` because the week is nearly over, while Codex at **82%** is
`slow_down` because five days remain. The percentage alone would have ranked
them the other way round.

Columns are only ever appended, never inserted, so scripts written against an
older version keep working.

If a CLI is not installed, `--check` emits a distinct status row such as
`claude NOT_INSTALLED ...` instead of pretending a fetch failed. One working
provider is enough for a successful exit; the command exits non-zero when an
installed provider could not be read, or when neither supported CLI is
installed.

Add `--pretty` for the same data with bars, when a human is reading.

### JSON

`afg --json` emits an envelope: **one directive per meter**, plus the full
per-provider detail if you want to look closer. Both providers are normalised
into the same shape, so consumers never need to know which API a number came
from. Every provider record includes `installed`: an absent CLI remains in JSON
with `installed: false`, an explanatory `error`, and no gauges or directives.
That lets an integration distinguish "not used on this machine" from "installed
but temporarily unavailable" without scraping prose.

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
| `provider`, `label` | which meter this applies to |
| `governs` | **`true` when this meter is the tightest constraint on the work it covers.** A subscriber steering its own rate should filter on this, or use `effectiveRateAdjustment` |
| `effectiveRateAdjustment` | the multiplier that actually applies once every meter covering this work gets a vote — never larger than `rateAdjustment`; capped at `1.0` when a young overlapping meter has not cleared an increase |
| `heldBy` | the meter overruling this one, when `governs` is false |
| `verdict` | `slow_down` / `on_track` / `spare_capacity` / `exhausted` / `too_early` — this meter's own reading |
| `direction` | `down` / `up` / `hold` — the arrow, as a value |
| `changePercent` | signed change to make, or `null` when no change is called for. `-92.2` = ease off by 92% |
| `rateAdjustment` | the same instruction as a multiplier, for code that scales a rate directly. `0.078` = throttle to 7.8% |
| `actionable` | `false` when the window is too new to judge |
| `projectedUsagePercent` | where you land at reset if nothing changes. Over 100 means you run out early |

**Use `effectiveRateAdjustment`, not `rateAdjustment`,** unless you have a
reason not to. The per-meter figure describes that meter in isolation, and
meters aren't isolated: a 5-hour window reporting `2.5` while the week reports
`0.34` means you may go at `0.34`, not `2.5`. Only the effective figure has
already reconciled them.

**There is deliberately no single combined instruction.** An earlier version
ranked every meter together and emitted one winner with one multiplier. That
was wrong twice over: ranking implies a prediction the data can't support —
`used / elapsed` is an *average since the window opened*, so a meter burned
hard days ago and idle since looks identical to one burning steadily now — and
a lone multiplier reads as advice about your whole workload when it only ever
described one window.

A window that has only just opened reports `too_early`, `actionable: false` and
`rateAdjustment: 1.0` — a safe no-op. Minutes of data divide into a wild ratio,
and throttling on that would be worse than doing nothing. Its uncertainty also
caps overlapping `effectiveRateAdjustment` values at `1.0`: hold is safe;
speeding up without weekly evidence is not.

#### Per-gauge detail

Each provider under `providers[]` carries `installed`, and each gauge under
`providers[].gauges[]` carries `window`, `scope`, `label`,
`percent`, `severity`, `activeLimit`, `resetsAt`, `secondsRemaining`,
`windowSeconds`, and its own `pace` object.

```sh
afg --json | jq -r '.directives[] | select(.governs and .direction == "down")
                    | "\(.provider) \(.label): \(.changePercent)%"'
afg --json | jq -r '.providers[].gauges[] | select(.activeLimit)
                    | "\(.label) \(.percent)%"'
```

### Subscribing

`afg --watch --json` keeps sampling and emits **one JSON object per line**,
flushed immediately — so anything that can read a pipe can subscribe:

```sh
afg --watch --json | while read -r line; do
  jq -r '.directives[] | select(.governs and .verdict == "slow_down")
         | "throttle \(.provider)/\(.label) to \(.effectiveRateAdjustment)x"' <<<"$line"
done
```

That's the whole mechanism: no daemon, no socket, no broker.

### Rate limiting

The vendor usage endpoints are themselves rate-limited, and polling them hard
earns a 429 long before your real quota runs out. Readings are therefore cached
on disk and shared between every `afg` process: even a status bar checking once
a second normally costs one request every five minutes. A cross-process lock
also collapses callers that reach an expired cache together into one upstream
request, including simultaneous presses of `r`.

A 429 records its `Retry-After` when supplied, plus the local PID and whether
an automatic poll or manual refresh triggered it. Without a longer server
deadline, a repeating incident backs off for 2, 4, 8, then at most 15 minutes;
a brief success does not immediately forget the pattern. Automatic polls wait
out that deadline. A later `r` remains one deliberate recovery probe, so it can
confirm that a provider recovered without opening the door to every caller.

```sh
afg --max-age 300     # the default: reuse a reading up to 5 minutes old
afg --no-cache        # always call the API (can earn you a 429)
afg --clear-cache     # drop cached readings and any standing backoff
```

Fresh polls also append one JSONL sample per gauge under
`$XDG_CACHE_HOME/agents-fuel-gauge/history/` (normally
`~/.cache/agents-fuel-gauge/history/`). With `AFG_CACHE_DIR` set, history lives
under `$AFG_CACHE_DIR/history/`. Repeated unchanged values within five minutes
are collapsed, corrupt lines are ignored, and samples older than 14 days are
pruned automatically.

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

Up to two read-only `GET`s to the installed providers' own usage endpoints,
using tokens already on your machine. A provider whose CLI is absent is never
contacted:

| Provider | Endpoint |
| -------- | -------- |
| Claude | `api.anthropic.com/api/oauth/usage` |
| Codex | `chatgpt.com/backend-api/wham/usage` |

Nothing is sent anywhere else. No telemetry, no server, no third party. The
response cache and local percentage history described above are the only data
written to disk, both under your XDG cache directory. Account emails are
masked and paths render as `~/…`, so screenshots stay shareable.

## Notes

- **Codex doesn't grade its own limits**, so severity is derived from
  thresholds (≥60% warning, ≥85% critical). Claude reports its own. Neither
  gets an "in force" flag invented for it: Anthropic reports one, OpenAI
  doesn't, so no Codex bar claims to be the binding one.
- **Codex identifies windows by duration, not name.** On plans with no 5-hour
  throttle, no 5h bar is drawn because the API doesn't return one.
- **Codex reports a plan family, not a product name.** `pro` and `prolite` are
  ChatGPT **Pro 20x** and **Pro 5x** — two different subscriptions at two
  different prices — so the raw value is mapped back to the name you actually
  pay for. Anything unrecognised is tidied and shown as-is rather than guessed
  into a plan we know.
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

Release notes are kept in [CHANGELOG.md](CHANGELOG.md).
