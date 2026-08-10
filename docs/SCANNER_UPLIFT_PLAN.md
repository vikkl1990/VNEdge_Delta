# Scanner Uplift Plan

VNEDGE scanners are split into three layers. Keep those layers separate:

- **Live observation scanners** answer "what is forming now?"
- **Research miners** answer "what looks interesting historically?"
- **Execution proof filters** answer "what survives quote, fill, fee, and replay?"

No scanner is allowed to promote or trade by itself. Every scanner payload must keep
`can_trade=false` and `can_promote=false`; promotion remains replay -> shadow/paper
-> human gate -> live ladder.

## Cross-Cutting Refactor

1. **Scanner telemetry contract**
   - Every runtime strategy must publish `features`, `thresholds`, and `proximity`.
   - Threshold extraction must read both direct strategy attributes and frozen
     `strategy.params` dataclasses.
   - Missing telemetry is an instrumentation defect, not a market verdict.
   - Runtime scanner rows must include `gate_diagnostics` and `uplift`, so the
     operator sees whether the blocker is side separation, participation,
     displacement, BBP pressure, cost edge, stale data, or missing telemetry.

2. **State contract**
   - Use `FIRING`, `NEAR_TRIGGER`, `WAITING`, `WARMING`, `STALE`, `NO_EVAL`.
   - Stale means latest `lane_eval` is old, even if the journal has newer restore
     or report records.
   - Paper-observation lanes should be shown separately from active live scanners.

3. **Execution truth contract**
   - Candle/event candidates are hypotheses only.
   - Tick/L2 candidates need conservative replay.
   - Replay failures must be bucketed as data gap, no quote, no fill, fee wall,
     queue risk, adverse selection, or low PF/net bps.

4. **Operator answer contract**
   - Every scanner artifact should include a short `operator_answer`.
   - The answer must say whether the next action is fix data, record more, replay,
     wait for signal, repair exits, or pre-register judgment.
   - Promotion-readiness rows must include `triage`, separating no-outcome,
     early negative, stop-dominated, mature negative, low-PF positive, paper
     waiting, and paper-running states.

## Runtime Scanners

### `realtime_scanner`

Current role: reads live `lane_eval`, `shadow_intent`, paper order, and event
lead-lag shadow journals.

Uplift:
- Expand proximity from funding/z only to score, score delta, TQI, quality
  strength, BBP, volume, body impulse, and expected net edge.
- Classify each primary blocker into a gate category and action:
  `REQUIRE_CLEARER_SIDE_DOMINANCE`, `WAIT_FOR_REAL_PARTICIPATION`,
  `WAIT_FOR_TRUE_IMPULSE_CANDLE`, `REPAIR_EXECUTION_ROUTE_OR_SKIP`,
  `FIX_DATA_FRESHNESS`, or `EXPOSE_THRESHOLD_TELEMETRY`.
- Distinguish stale paper-observation lanes from stale active shadow lanes.
- Add per-row `uplift`: `FIX_DATA_FRESHNESS`, `WAIT_MARKET`,
  `EXPOSE_THRESHOLD_TELEMETRY`, `WATCH_NEAR_TRIGGER`, `OBSERVE_PAPER`.

### `event_leadlag_shadow`

Current role: watches the selected SOL/XRP lead-lag specs in live 1m context.

Uplift:
- Include threshold/proximity in every no-trade row.
- Add duplicate suppression counters and last eligible event timestamp.
- Separate "leader event not present" from "follower already moved" and
  "volume confirmation missing".

### `realtime_shadow_scalp`

Current role: runs cascade and lead-lag echo families in virtual execution.

Uplift:
- Publish fee-wall decomposition: gross move, maker cost, taker exit cost,
  slippage/safety buffer, net.
- Add family-level cooldown/decay so repeat negative-edge lanes do not dominate
  the cockpit.
- Require positive virtual edge before any manifest/paper discussion.

## Research Miners

### `event_leadlag_alpha`

Current role: mines cross-venue candle lead-lag hypotheses.

Uplift:
- Persist sample distributions, not only summary averages.
- Add leader/follower venue quality filters before ranking.
- Promote only to replay queue; never directly to runtime lane.

### `fast_l2_scout`

Current role: recent tick/L2 scout for interactive discovery.

Uplift:
- Make `UNDER_SAMPLED` actionable with required minutes/events per lane.
- Surface lane-filter blocker counts at row level.
- Tag results as `synthetic_observation_fill_not_replay` until candidate replay
  proves quote/fill behavior.

### `l2_research_loop`

Current role: slow conservative L2 loop over recorded days.

Uplift:
- Keep restart-safe per-target checkpoints visible in the dashboard.
- Add ETA and current target.
- Avoid publishing partial passes as equivalent to complete research verdicts.

### `orderflow_footprint`

Current role: compresses public trade tape into CVD/stacked-imbalance anomalies.

Uplift:
- Add pre/post footprint outcome slices before replay.
- Route all candidates to candidate replay with no promotion shortcut.
- Add score components to explain whether notional, imbalance, stack length, or
  price response carried the candidate.

### `daily_scalper_pack`

Current role: candle-based multi-timeframe scalper research.

Uplift:
- Split "positive smoke" from "promotion pass" more visibly.
- Promote refactor candidates only after untouched judgment.
- Track why strict profiles under-fire: context filter, trigger filter, fee drag,
  or too few trades.

### `daily_scalper_cadence`

Current role: finds higher-cadence lane refactors.

Uplift:
- Treat cadence as a constraint, not the objective.
- Add "more trades but lower expectancy" veto.
- Queue candidates into shadow refactor only when net/PF/cadence all improve.

### `alpha_distillation`

Current role: distills profitable factor atoms into a compact lane.

Uplift:
- Add atom-level attribution into the scanner result.
- Block single-lucky-trade atoms from workbench escalation.
- Require fresh judgment before any runtime adapter is built.

### `cascade_reversion`

Current role: scans cascade impulse/reversion families.

Uplift:
- Move negative-edge families into explicit cooldown/tombstone lifecycle.
- Publish sample sufficiency and fee-wall gap.
- Route only replay-positive cascades to shadow scalp.

### `leadlag_echo_scalp`

Current role: historical tick-level echo scalp family.

Uplift:
- Stop one-shot restart loops from producing stale/noisy artifacts.
- Publish current-day freshness separately from historical verdict.
- Require maker fill evidence and positive net before runtime shadow.

## Execution Proof Filters

### `candidate_replay_executor`

Current role: passive quote, conservative trade-through fill, taker exit, honest
fees.

Uplift:
- Add explicit `failure_bucket` on each row, not only verdict.
- Group repeated event specs so one candidate does not flood the result table.
- Preserve data-gap failures as "record more", not "negative edge".

### `execution_condition_miner`

Current role: explains replay failures and proposes fresh filters.

Uplift:
- Standardize row and candidate-level fields: `reason_bucket` for rows,
  `primary_bucket` for grouped candidates.
- Feed bucket labels into Alpha Council and UI consistently.
- Keep every proposed filter marked `must_replay_fresh_window=true`.

### `filtered_replay_executor`

Current role: applies causal pre-entry filters on fresh/unseen replay windows.

Uplift:
- Explain `SEEN_REPLAY_WINDOW_EXCLUDED` in operator terms.
- Add `waiting_for_fresh_window` summary when all rows are excluded.
- Output shadow-trial manifests only for replay-positive unseen rows.

## Governance Scanners

### `lane_promotion_readiness`

Current role: truth table for paper/shadow/live readiness.

Uplift:
- Split `PAPER_ACTIVE` from `PAPER_REVIEW_READY`.
- Show exact blockers: trades, days, PF, net, paper verdict, live ladder.
- Add `triage.bucket` and `triage.action`:
  `EARLY_STOP_DOMINATED` means refactor entry/exit quality before promotion;
  `MATURE_NEGATIVE_EDGE` means disable paper promotion until refactored;
  `NO_LIVE_OUTCOMES` means keep observing and inspect realtime scanner;
  `APPROVED_PAPER_RUNNING` remains visibility only, not promotion.
- Never let runtime firing count imply live-readiness.

### `alpha_council`

Current role: ranks candidates and creates next research actions.

Uplift:
- Treat stale artifact health as first-class work.
- Prefer execution-condition repair over threshold tuning.
- Add separate queues for judgment, replay, data collection, and runtime adapter
  work.

### `alpha_workbench`

Current role: task queue generated from council debates.

Uplift:
- Add task aging and blocked-reason decay.
- Pin every task to source artifact, data window, and promotion boundary.
- Prevent repeated tasks for the same seen-window failure.

### `vibe_intelligence`

Current role: lifecycle memory for hypotheses.

Uplift:
- Track hypothesis state transitions: incubating, active, monitoring, decayed,
  disabled.
- Disable ideas that repeatedly fail replay with the same reason bucket.
- Keep resurrected ideas behind fresh-data requirements.

### `bitcoin_regime`

Current role: external stress/regime context.

Uplift:
- Label as context only; not a direct signal.
- Add source-health state and stale/degraded output.
- Feed regime split requests into Alpha Council, not live runtime.

## Immediate Priority

1. Refactor quant/alpha-stack lanes with stop-dominated shadow outcomes before
   any paper promotion discussion.
2. Reduce side-conflict lanes: the VM snapshot shows side separation as the top
   runtime blocker, so prefer family/side isolation over threshold loosening.
3. Fix stale runtime lanes before tuning; stale rows are data/runtime defects.
4. Add failure bucket consistency from replay -> council -> UI.
5. Only then discuss threshold changes.

## VM Snapshot: 2026-07-16

Using the live VM journals copied read-only from `161.118.252.185`, the gate
refactor classified the active scanner estate as:

- Realtime scanner: 62 rows, 0 firing, 1 near-trigger, 49 waiting, 12 stale.
- Top runtime blockers: side separation 18, trend quality 7, participation 5,
  displacement 4, crowding/extension 6 combined.
- Readiness triage: 7 `EARLY_STOP_DOMINATED` shadow lanes, 4
  `NO_LIVE_OUTCOMES`, 9 approved paper lanes running, 20 paper lanes waiting.

Trading interpretation: do not loosen gates. The elevation path is stricter
side/family isolation, cleaner impulse/participation filters, execution proof,
and exit-geometry repair for the stop-dominated lanes.
