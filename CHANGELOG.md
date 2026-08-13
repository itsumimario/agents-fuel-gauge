# Changelog

Notable user-facing changes to agents-fuel-gauge are recorded here. Releases
before 0.8.0 were documented in the Git commit history.

## 0.10.1 - 2026-08-13

### Changed

- Reworked the synthetic history screenshots around one directly comparable
  Claude trace: sustained heavy use, a sparse quiet stretch, and a short burst
  followed by a long plateau. Recorded, Full, and Details now show that same
  sample series so the differences between view modes are clear.

## 0.10.0 - 2026-08-13

### Added

- Added a history-only `d Details` view that expands up to three inferred rate
  segments per provider into individually scaled, newest-first plots. Each
  segment shows its time range, percentage change, duration, and fitted daily
  rate; `d` becomes `Overview` while viewing them.
- Added deterministic synthetic README screenshots for Recorded, Full, and
  Details history views.
- Added a short README guide to reading History and choosing between Recorded,
  Full, and Details.

### Changed

- The history legend switches its gray-line explanation from ideal budget pace
  to fitted segment rate in Details.
- The `z Zoom` action is hidden in Details, where each segment already has its
  own viewport, and the default overview range is now labeled `Recorded`.
- Documentation screenshots now preserve the dashboard's semantic colors even
  when regenerated in an environment that requests monochrome output.

## 0.9.1 - 2026-08-13

### Fixed

- Masked provider accounts no longer flash at mount and disappear on the first
  one-second freshness update. Narrow panels now drop the plan first and use a
  compact age before sacrificing the account.
- The `z Zoom` action is now shown only in history, where it has an effect.

### Release

- Restored annotated release tags for every untagged version from 0.5.1 onward.

## 0.9.0 - 2026-08-13

### Added

- Added an always-visible history legend for normal, warning, and critical
  usage traces, ideal budget pace, and the required path to reset.

### Changed

- The theme footer button now says `Light` in dark mode and `Dark` in light
  mode, describing the action it will take.
- Relabeled Textual's command palette as `Options` and moved its shortcut from
  `Ctrl+P` to the easier `o` key.
- Footer actions now wrap into two clickable rows when the terminal is too
  narrow for one row.
- Detailed history now fits both axes to the recorded interval, avoiding empty
  days before late-window traces and empty future days after early-window
  traces. The `z` full-window view is unchanged.
- Updated the README key map, responsive-footer behavior, history viewport,
  graph colors, and installation dependency notes.

## 0.8.0 - 2026-08-13

### Added

- Added an explicit `installed` field to each provider in JSON output and a
  `NOT_INSTALLED` status in command-line output, so integrations can distinguish
  an absent CLI from an installed provider that is signed out or unavailable.
- Added `z` in the history view to toggle between its detailed and full-window
  ranges.

### Changed

- The live dashboard now omits providers whose official CLI is not installed.
  Installed providers continue to show authentication, network, rate-limit, and
  quota problems, and a generic empty state explains when neither CLI exists.
- History now opens in a phone-friendly detailed viewport fitted to the recorded
  tail and the remaining path to reset. The prior whole-window view remains
  available with `z`; neither view changes pace or directive calculations.
- The history shortcut is now `h` instead of `p`.
- Updated the README for provider visibility, output status, history zoom, and
  the complete live-dashboard key map.
