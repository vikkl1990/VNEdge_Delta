# ML integration — pre-registered program (2026-07-26)

A high-quality ML integration for a trading bot is defined by its **validation
discipline**, not its model complexity. The first ML attempt overfit (IS
+$1,343 vs OOS −$18.50) and was correctly rejected. This program builds three
ML roles on one shared, anti-overfit foundation, each promoted only through the
existing gated path. **Pre-registered — do not edit success criteria after
seeing results.**

## Invariants (unchanged, apply to every role)

- A model trades **only** as a `BaseStrategy` via `MLStrategy`: the pre-trade
  gateway, sizing, journal, kill switch, and mode ladder all apply. No model
  places or sizes an order outside the gateway.
- Nothing trades a model that isn't in the versioned `model_registry`.
- Features are **causal** (`feature_matrix`, causality unit-tested). Labels look
  forward; leakage discipline lives in the **split** (purge + embargo).
- Reduce-only exits are never gated by a model.
- Judgment runs **only** on pre-registered untouched windows.

## Shared foundation (build first — serves all three roles)

1. **Robust validation** (`ml/validation.py`, this PR): Probabilistic & Deflated
   Sharpe (multiple-testing aware), Probability of Backtest Overfitting (CSCV),
   and Combinatorial Purged CV (many purged/embargoed OOS paths).
2. **Extended causal features**: add the fee-wall / microstructure signals
   (expected-net-edge bps, spread, funding percentile, regime, realized vol,
   session) to `feature_matrix`, all causal, all unit-tested.
3. **Calibration**: isotonic/Platt so a probability of 0.7 *means* 0.7 — required
   for principled sizing and for the meta-label gate.
4. **Robustness engineering**: feature-drift (PSI) + performance-decay monitors
   with retrain triggers; **fail-safe fallback** to the rule-based signal when a
   model is stale/unavailable/drifted (never blocks exits).

## Locked promotion bar (every role, before untouched judgment)

- CPCV out-of-sample: **median OOS PF ≥ 1.3** across folds, positive net after
  costs, DD within the rule-based envelope.
- **Deflated Sharpe ≥ 0.95** on the aggregated OOS path (given the trial count).
- **PBO ≤ 0.20** across the config family.
- **Must beat the rule-based baseline** OOS (the existing signal / regime
  classifier) — an ML role that doesn't beat the simpler thing is rejected.
- If any gate fails, **stop and report** — do not tune to pass (that IS the
  overfit trap). The catalog offers no "make it pass" knob.

## The three roles (risk-ordered; each is a separate, gated PR)

### ① Meta-labeling — FIRST (lowest risk, builds on the existing edge)
The evidence-aligned rule-based signals stay the **primary** (direction/entry).
A secondary calibrated classifier predicts **P(this signal wins after costs)**
and gates/sizes the trade. Directly attacks the bot's real failure mode
(fee-wall false positives). Label = triple-barrier outcome *of the primary
signal's trade*, net of fees. Trains on historical grid + accumulating
paper/shadow trade outcomes. Baseline to beat: taking every primary signal.

### ② Regime / permission layer — SECOND
A classifier labels market regime and sets per-strategy permission
(ALLOW/BLOCK/REDUCE/SHADOW_ONLY). **Must beat the existing rule-based regime
classifier** (`strategy/regime.py`) OOS through this machinery, or it is
rejected. Complements the signals; never a standalone entry.

### ③ Standalone direction — LAST (highest risk)
The current `MLStrategy` path: predict up/down independently. This is the setup
that overfit; it runs only after the validation rig is proven on ① and ②, on a
pre-registered untouched window, long-only v1. Most likely to fail OOS honestly
— and that's an acceptable outcome.

## Data honesty

- The second-eye grid data is **seen** (it set the roster). Training on it then
  judging on it is overfitting. It may seed models, but the **judgment window is
  pre-registered and untouched**, and the accumulating live paper/shadow trades
  are the honest forward validation set.
- Meta-labeling needs the primary signals' realized outcomes — these grow as the
  5 paper trials mature (weeks). Until then, work stays on the foundation +
  historical CPCV with a locked untouched holdout.

## Sequence

Foundation (validation ✓ this PR → features → calibration → monitoring) →
① meta-label (shadow → beat baseline OOS → untouched judgment → paper → ladder)
→ ② regime → ③ standalone. Nothing trades until it clears the locked bar above.
