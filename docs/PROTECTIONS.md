# Entry protections — post-stop cooldown & stop-window guard

`src/vnedge/risk/protections.py` is a pure state machine that both execution
engines consult before every **entry** decision:

| Protection | Config field(s) | Behavior when enabled |
| --- | --- | --- |
| Post-stop cooldown | `cooldown_bars_after_stop` | After a STOP exit (`stop` / `tick_stop`), entry evaluations are blocked for N bars starting with the stop bar itself. `1` blocks exactly the same-bar re-entry. |
| Stop-window guard | `max_stops_per_window` + `stop_window_bars` (enabled together, or not at all) | While ≥ N stop exits sit inside the trailing window, entries stay blocked until the oldest stop ages out. |

**Everything defaults to OFF (`0` = disabled).** A default `ProtectionConfig`
changes nothing — there is an explicit zero-behavior-change test
(`tests/test_protections.py::test_backtest_defaults_off_zero_behavior_change`).

## Where it plugs in

- **Live paper/shadow runner** — `RunnerConfig.protections`. The legacy
  `RunnerConfig.post_exit_cooldown_bars` (PR #92) survives as a back-compat
  alias that folds into `cooldown_bars_after_stop` (the stricter value wins).
  Blocked bars journal a `lane_eval` record with the block reason as
  `skip_reason`; the dashboard trade log gets one `protection_blocked` event
  per blocking episode.
- **Backtester** — `BacktestConfig.protections`. Blocked decisions are
  returned as `BacktestResult.protection_blocked` (timestamp, reason) pairs.
- **Engine parity** — both engines feed the *same* state machine with the
  same bar-index semantics; a parity test drives one synthetic stop sequence
  through both and asserts identical blocked decisions
  (`tests/test_protections.py::test_engine_parity_same_blocked_decisions`).
  Research and operations cannot disagree about what a protection would block.

Semantic refinement vs PR #92: the cooldown arms on **stop exits only**.
A take-profit or max-holding close is not evidence that the entry condition
went bad, so winners no longer suppress re-entry.

## Invariants

- **Exits are never affected.** The state machine has no exit-blocking API at
  all; reduce-only exits flow through the normal gateway/journal pipeline
  untouched. Capital protection beats entry hygiene, always.
- **Every block is explainable.** The reason string carries *every* active
  block (`post_exit_cooldown: …; stop_window_guard: …`), not just the first.
- **Frozen config.** Like every risk config, `ProtectionConfig` is immutable;
  changing a limit means a restart, never a mid-flight mutation.

## Governance: default-off, pre-registration required

Enabling any protection **changes entry behavior**. Running trials operate
under frozen protocols, so a protection may only be switched on through a
pre-registered protocol for a *future* trial or research round — never applied
mid-trial, and never retroactively to promote a seen result.

### The motivating example cuts both ways — say so

The reason this module exists: **2026-07-06**, the live paper session stopped
out of a short and the still-armed condition re-fired an entry **on the very
same bar** that had just proven the prior trade wrong. That felt obviously
pathological — and the same-bar re-entry **went on to WIN**. Blocking it would
have cost money.

That is the whole lesson: protections are **hypotheses to test, not obvious
improvements**. Intuition ("surely don't re-enter straight after a stop") is
exactly the kind of plausible-sounding rule that must earn its place through
the same walk-forward + promotion-gate machinery as any strategy change. Until
a pre-registered test shows a protection improves OOS results for a given
lane, it stays off.

## Research loop

When a rolling REJECT shows stops clustering in consecutive runs
(`max_consecutive_stops >= 3` in the `wf_record`), the diagnostics engine
proposes a whitelisted protection variant — `cooldown_bars_after_stop` in
`[3, 6]`, axes namespaced `protections.*` so they can never be mistaken for
strategy constructor params. These proposals are **`auto_runnable=False`**:
they appear on the research feed for a human to consider, and the bounded
auto-explorer never runs them. Testing one is a deliberate, pre-registered
decision like any other engine/config change.
