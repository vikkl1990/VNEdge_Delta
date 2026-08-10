# Pine Source Corpus Review - 2026-07-18

Status: research-only. No row in this corpus can trade, promote, or bypass the
VNEDGE promotion ladder.

## Intake State

The TradingView/Pine lab now has a real source-backed corpus instead of only
catalog metadata:

- 908 discovered records in the generated KB.
- 496 browser extraction attempts recorded in the local manifest.
- 432 successful open-source extraction attempts.
- 427 unique `.pine` source files present in the gitignored source store.
- 481 rows still catalog-only and blocked until public/open-source source is
  available.
- 62 retryable browser errors and 2 source-tab extraction failures. These are
  not strategy verdicts; they are intake failures to retry in smaller chunks.

Source files and manifests stay out of Git. The committed KB stores provenance,
hashes, line counts, verdicts, and queues only.

## Source-Backed Mechanisms

Current source-backed mechanism mix:

| Mechanism | Count | VNEDGE Read |
|---|---:|---|
| breakout | 148 | Most common. Needs strict room-to-liquidity and fee-aware execution filters. |
| liquidity | 86 | Best fit for your SMC/FVG/OB/sweep vision, but must be causal and non-repainting. |
| structure | 83 | Useful for CHoCH/BOS/swing context and stop placement. |
| momentum | 58 | Better as an edge-model feature than a standalone signal. |
| orderflow | 52 | Valuable only when mapped to real trade/book data, not chart-only volume labels. |

Source-backed verdicts:

| Verdict | Count | Meaning |
|---|---:|---|
| PORTABLE | 187 | Mechanism can be ported into VNEDGE for causal feature/replay work. |
| PORTABLE_WITH_CHANGES | 121 | Useful, but needs causality/execution adaptation before replay. |
| BLOCKED_REPAINT_RISK | 117 | Do not port until the MTF/lookahead/display-state risk is removed. |
| RESEARCH_ONLY | 2 | Keep as concept/feature only. |

## What This Teaches Us

The source corpus confirms the scanner problem: most popular visual indicators
are not directly executable alpha. They are a mix of display state, late labels,
MTF overlays, and unpriced TP/SL geometry. VNEDGE should not copy them as
signals. It should distill their recurring causal primitives and test those
primitives against fees.

The promising primitive families are:

1. Liquidity-zone continuation: FVG/order-block/supply-demand zone, then
   displacement or close-confirmed breakout with volume participation.
2. Sweep-then-reclaim: wick sweep of recent swing/external liquidity, close back
   inside structure, then continuation trigger.
3. Session/range expansion: opening/session box compression, break, retest, and
   room-to-next-liquidity target.
4. Adaptive trail exits: ATR/Supertrend/Kalman/chandelier-style trail as exit
   management, not as a raw entry edge.
5. Momentum as confirmation: RSI/MACD/BBP/volume-z/ADX/ER should be features in
   the edge router, not hard-coded binary gates.
6. Orderflow proxies: CVD/delta/volume-profile ideas need VNEDGE trade prints,
   L2 snapshots, and conservative fills. Pine-only approximations are not proof.

## Do Not Port Blindly

Reject or quarantine these before backtesting:

- Any `lookahead_on` usage.
- Any `request.security` logic that is not rewritten to use only closed HTF bars.
- `barstate.islast` dashboards or labels used as if they were historical
  signals.
- Visual-only `plotshape`/table overlays with no machine alert or execution
  contract.
- Session logic designed for equities/FX that assumes market open/close unless
  it is explicitly adapted to 24/7 crypto.
- Fixed TP/SL visuals that do not include taker/maker fees, slippage, and
  minimum expected net bps.

## Recommended Build Order

1. `pine_alpha_distiller_v1`: built as the source-backed primitive/task
   distiller. It parses accessible Pine artifacts into zone, sweep,
   displacement, volume impulse, MTF bias, trail, target/stop geometry, and
   repaint-risk queues without emitting Pine source.
2. `fvg_liquidity_breakout_v1`: built as the first executable port family from
   the corpus. It uses 1h bias, 15m setup, 5m trigger, FVG retest/sweep/structure
   events, volume/displacement confirmation, smart capture metadata, and a 25 bps
   expected-net floor.
3. `trail_exit_lab_v1`: evaluate ATR/Supertrend/Kalman/chandelier exits across
   existing VNEDGE entries. Exit quality may improve faster than entry alpha.
4. `orderflow_proxy_v1`: convert source-backed CVD/delta/volume-profile ideas
   into features backed by real public trades and L2 recorder data.
5. `pine_intake_retry_chunks`: retry the 62 browser errors and continue the
   remaining catalog-only queue in smaller chunks. Protected/no-source rows stay
   blocked.

## Current Honest Verdict

This corpus improves our research funnel, not the live bot yet. It gives VNEDGE
hundreds of source-backed ideas to distill, but every profitable claim still has
to survive:

source review -> causal Python port -> multi-timeframe replay -> cost-aware
route proof -> untouched-window judgment -> shadow/paper trial.
