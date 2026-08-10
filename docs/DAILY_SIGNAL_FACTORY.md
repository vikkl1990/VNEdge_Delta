# Daily Signal Factory Contract

VNEDGE can run lanes as an intraday signal factory instead of a position
holding system. The contract is deliberately simple:

- No new entries after the configured session cutoff.
- No open exposure past the configured force-flat minute.
- Limit entries per lane per session.
- Optionally stop new entries once the daily profit target is banked.
- Backtest and live-paper use the same clock rules.

This is the operating mode for daily crypto scalping: generate many candidate
signals, accept only the best route-valid ones, manage the trade actively, and
close the book before the day ends.

## Config

The shared policy is `DailySignalFactoryConfig`:

- `enabled`: default `false`, so existing judged trials keep their behavior.
- `session_timezone`: default `UTC`.
- `entry_cutoff_minute`: local minute after which entries are blocked.
- `force_flatten_minute`: local minute at/after which positions are closed.
- `max_entries_per_day`: per-lane daily entry cap.
- `daily_profit_target_usd`: optional daily stop-after-profit threshold.
- `cancel_resting_entries_at_cutoff`: cancel unfilled maker entries at cutoff.
- `flatten_open_positions`: force reduce-only close before session end.

The cutoff must be before the force-flat minute.

## Why this matters

The prior paper ledger showed too many trades giving back MFE and carrying
unclear exit intent. The daily factory makes that impossible at the runner
level: even if a scanner keeps believing the setup, the session contract wins.

Research still decides whether a scanner has edge. The daily factory decides
whether the edge is allowed to become an overnight liability.
