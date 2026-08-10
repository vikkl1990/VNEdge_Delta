# Execution Edge Labeler

`vnedge.research.execution_edge_labeler` is the missing truth layer between
chart scanners and promotion.

It answers the executable question:

> after the next fill, route cost, stop/target path, MFE/MAE, and maker-fill
> assumption, did this signal still have post-fee edge?

## Why this exists

Public scanner stacks, yFinance-style candle research, and generic forecasting
models can produce attractive signals without proving tradability. For VNEDGE,
a signal is not useful until it survives:

- next-open/taker route cost or maker-first route cost
- stop-first intrabar resolution
- MFE/MAE and R-multiple path quality
- fill probability, especially for maker-first scalps
- profit factor and average net bps after cost

This layer is research-only. It never places orders, never promotes, and never
turns a maker candle touch into fill proof. Maker labels are explicitly marked
as assumed unless replay or live shadow evidence supplies a fill probability.

## CLI

```bash
python -m vnedge.research.execution_edge_labeler \
  --data-root data \
  --exchange delta_india \
  --symbol ETH/USD:USD \
  --timeframe 5m \
  --strategy sats_5m_scalper_v1 \
  --route MAKER_ONLY \
  --lookback-days 30 \
  --json
```

The report contains:

- `summary.verdict`: `NO_EVENTS`, `UNDER_SAMPLED`, `LOW_FILL_CONFIDENCE`,
  `NEGATIVE_AFTER_COST`, `MAKER_EDGE`, or `TAKER_EDGE`
- `avg_net_bps`, `profit_factor`, `target_rate_pct`, `stop_rate_pct`
- per-event `gross_bps`, `net_bps`, `mfe_bps`, `mae_bps`, `max_r`, `min_r`
- `fill_evidence`: `taker_next_open`, `maker_supplied`, or `maker_assumed`

## Leaderboard overlay

Research rows may attach either the full report or just the summary:

```json
{
  "strategy": "sats_5m_scalper_v1",
  "verdict": "PASS",
  "execution_truth": {
    "summary": {
      "verdict": "NEGATIVE_AFTER_COST",
      "samples": 44,
      "avg_net_bps": -2.8,
      "profit_factor": 0.72,
      "primary_blocker": "average net/PF below maker breakeven"
    }
  }
}
```

`edge_leaderboard` will then block the promotion queue even if rolling research
looked good. Positive truth labels (`MAKER_EDGE` / `TAKER_EDGE`) can improve
route annotation, but still cannot approve paper/live without untouched
judgment and human approval.

## Operating rule

When scanners are not firing, or fired signals lose money, diagnose in this
order:

1. No events: scanner threshold/context is too sparse.
2. Low fill confidence: maker-first route is not proven by replay/shadow.
3. Negative after cost: raw pattern exists but fee/adverse path kills it.
4. Maker edge: collect replay/live shadow fill evidence.
5. Taker edge: candidate for strict judgment, not automatic trading.
