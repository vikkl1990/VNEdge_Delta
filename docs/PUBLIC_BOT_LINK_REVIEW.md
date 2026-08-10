# Public Bot Link Review

This note records what VNEDGE can safely learn from the public bot and strategy
links supplied on 2026-07-16. The rule is simple: use durable design patterns,
never copy strategy code, never treat public backtests as proof of edge.

## What Transfers

- **Lifecycle discipline** from Zenbot, Magic8Bot, and Gekko: separate live
  feature updates from closed-candle decisions, expose warmup/preroll states,
  and keep paper/live execution visibly distinct.
- **MTF event chaining** from WolfBot-style strategies: let 4h/1h context
  enable or block 15m/5m/1m trigger lanes, then show the chain health in the
  signal funnel.
- **Strategy zoo indexing** from Freqtrade Strategies, Gekko Strategies, and
  Mynt: keep a benchmark database of every tested family, pair, timeframe,
  rejected config, and fee-adjusted result so research does not keep circling
  back to the same dead shapes.
- **Execution motifs** from WolfBot and Freqtrade: maker-first routing, taker
  only when expected move clears fees/slippage/buffer, partial TP, BE after TP1,
  and trailing exits. VNEDGE already owns parts of this; the missing piece is
  route and exit quality telemetry across lanes.
- **Perp context** from WolfBot-style OI/funding monitors: funding exists in
  VNEDGE, but OI availability and quality need to become visible lane blockers.
- **Visual lineage** from Superalgos: useful as a dashboard/workbench idea, not
  as an execution architecture import.

## What Does Not Transfer

- Old public strategy parameters are not evidence. Most linked repos are spot
  era, stale, or educational.
- Microservice decomposition is not a win for VNEDGE v1. The current
  single-process execution invariant is safer for portfolio/risk state.
- Operator chat commands must stay read-only first. No Telegram or Discord
  start/stop/live controls until audit logging and permissions are proven.

## Immediate Build Queue

1. `strategy_benchmark_index_v1`: index all research/feed rows into a searchable
   public-style benchmark table ordered by untouched/paper evidence first.
2. `mtf_chain_health_report_v1`: show whether HTF bias, setup, trigger, and
   exit plan aligned or blocked every lane.
3. `exit_quality_scorecard_v1`: score trailing stop, TP1/TP2/TP3, BE, and
   taker fallback outcomes after fees.
4. `public_strategy_family_miner_v1`: rebuild BB/RSI/ADX/EMA scalp-bounce
   templates as owned causal atoms and subject them to VNEDGE walk-forward,
   replay, and burn-registry judgment.

The machine-readable version is produced by:

```bash
python -m vnedge.research.public_bot_inspiration
```

It publishes `research/live_research/public_bot_inspiration_latest.json` and an
append-only `public_bot_inspiration_feed.jsonl`, and continuous research folds
the same matrix into `latest.json` when `PUBLIC_BOT_INSPIRATION_ENABLED=1`.
