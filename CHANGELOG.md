# Changelog

Notable user-facing changes to agents-fuel-gauge are recorded here. Releases
before 0.8.0 were documented in the Git commit history.

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
