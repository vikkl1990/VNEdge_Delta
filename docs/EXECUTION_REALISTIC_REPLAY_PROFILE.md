# Execution-Realistic Replay Profile

`execution_realistic_replay_profile_v1` is a research-only publisher that
classifies every normalized evidence row by the execution proof it has actually
cleared.

## Why This Exists

Pine/TradingView-style scans can show that price moved after a signal. That is
not the same as proving VNEDGE could enter, fill, manage, and exit the trade.
The profile creates a visible ladder:

- `L0_SOURCE_OR_VISUAL_ONLY`: source or chart intention only.
- `L1_CANDLE_FORWARD_ROUTE_LABEL`: closed-candle path evidence, no order fill.
- `L2_NEXT_TRADE_TAKER_REPLAY`: taker fill on the next eligible trade.
- `L3_L2_TRADE_THROUGH_REPLAY`: maker fill only after trade-through evidence.
- `L4_L2_QUEUE_AWARE_MAKER_REPLAY`: maker fill after queue-ahead modeling.

Strict economics still means at least `25bps` net, `PF >= 1.5`, and `20`
samples. But strict economics alone does not make a lane paper-ready. Paper
review needs L3/L4 execution truth or a separately approved taker-only exception.

## Prediction-Market Settlement Review

The `evan-kolberg/prediction-market-backtesting` project is useful for VNEDGE in
its execution modeling discipline: L2 replay, trade ticks, queue/latency
modeling, ledger replay, coverage metadata, and portfolio scorecards.

Its settlement logic is not portable to crypto perpetuals:

- Binary terminal payoff is blocked.
- Complementary YES/NO pair arbitrage is blocked for a single perp.
- Hold-to-resolution is blocked because perps have mark-to-market PnL, funding,
  liquidation path risk, and explicit exits.
- Maker rebate assumptions are blocked unless they come from the actual account
  exchange fee tier.

Portable pieces are ledger replay, coverage/gap penalties, and portfolio-level
mark-to-market scoring.

## Running

```bash
python -m vnedge.research.execution_replay_profile \
  --evidence-index research/live_research/evidence_index_latest.json \
  --fee-wall research/live_research/fee_wall_forensics_latest.json \
  --candidate-replay research/live_research/candidate_replay_latest.json \
  --out research/live_research/execution_replay_profile_latest.json \
  --feed research/live_research/execution_replay_profile_feed.jsonl
```

Docker Compose runs the same publisher as `execution-replay-profile`. The Pine
Research Lab reads it through `/pine-research/execution-profile`.
