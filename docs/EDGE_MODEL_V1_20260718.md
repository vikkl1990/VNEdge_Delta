# Edge Model v1 Proof - 2026-07-18

Branch: `codex/edge-model-v1`  
Base dependency: `codex/edge-router-opportunity-labeler` / PR #189  
Runtime: VM Docker image `vnedge-research-loop` with `PYTHONPATH=/work/src`  
Deployment: not deployed

## What Was Built

`edge_model_v1` is a research-only learner over scanner opportunity rows.

Flow:

1. Generate raw scanner opportunity rows with `execution_edge_router_v1`.
2. Build a chronological feature/target table.
3. Train maker/taker net-bps regressors on the first 70% of rows.
4. Calibrate a selector on train rows only.
5. Apply the selector to the held-out 30% OOS tail.
6. Compare model-routed OOS performance against the raw all-scanner baseline.

Safety:

- `can_trade=false`
- `can_promote=false`
- no manifests written
- no runtime config changed
- no deployment performed
- OOS route decisions do not use forward truth

## Libraries Added

The PR adds an optional `quant-research` dependency bundle:

- `duckdb` for SQL over Parquet opportunity stores
- `polars` for fast lazy feature matrices
- `optuna` for bounded/pre-registered tuning
- `river` for future online shadow-learning
- `numba` for replay/MFE/MAE speedups
- `ruptures` for regime change detection
- `evidently` for drift monitoring
- `statsmodels`, `arch`, `skfolio` for statistical research, volatility, and
  portfolio allocation work

The first model implementation uses the existing sklearn dependency so the
production image does not require the optional research bundle.

## VM Proof Scope

Recorded candle data:

- Exchanges: `binanceusdm`, `bybit`, `delta_india`
- Symbols: BTC, ETH, SOL, BNB, XRP, DOGE
- Timeframe: `15m`
- Lookback: 30 days
- Horizon: 8 bars
- Opportunities: 10,534
- Train rows: 7,373
- OOS test rows: 3,161

Strict run:

- Min model trades: 20
- Min paper gate: avg net >= 25 bps and PF >= 1.5

| Metric | Raw Scanner Baseline | Edge Model v1 |
|---|---:|---:|
| OOS trades | 3,161 | 16 |
| Selection rate | 100.00% | 0.51% |
| Avg net bps | -13.3998 | -10.7997 |
| Profit factor | 0.6474 | 0.8127 |
| Win rate | 33.09% | 31.25% |
| Improvement | n/a | +2.6002 bps |

Strict verdict: `UNDER_SAMPLED`  
Reason: only 16 selected OOS model trades; need at least 20.

Diagnostic run with a 10-trade evidence floor:

- Verdict: `MODEL_IMPROVED`
- Same 16 OOS model trades
- Same +2.6002 bps improvement
- Still not a paper candidate because avg net and PF remain negative/low

## Timeframe Verification Uplift

Follow-up branch: `codex/edge-model-timeframe-uplift`

The proof harness now supports timeframe matrix runs:

```bash
python -m vnedge.research.edge_model_v1 \
  --data-root data \
  --timeframes all \
  --matrix \
  --compact
```

What changed:

- Adds explicit 1m/5m/15m/1h/4h matrix reporting.
- Keeps the old single-`--timeframe` CLI behavior intact.
- Adds cost/timeframe/risk geometry features that are available before the
  decision: timeframe seconds, cyclical hour/day context, maker/taker cost gap,
  expected-edge-minus-cost, expected-edge-to-risk, risk-to-cost, and
  fill-adjusted expected edge.
- Keeps forward truth out of features: maker/taker realized net bps, event id,
  and timestamp remain blocked from model inputs.

VM data availability check:

- 73 candle series present.
- 1m, 15m, 1h, and 4h exist for all 18 venue-pair lanes.
- 5m currently exists only for `delta_india:ETH/USD:USD`.

Full all-target/all-timeframe matrix generation exposed an infrastructure gap:
scanner opportunities are recomputed from candles for every proof. The
interactive full matrix did not finish within an operator-friendly window on
the busy VM. This is not an edge verdict; it is a research-infra finding. The
next build should persist scanner opportunity rows to Parquet/DuckDB so 1m and
all-lane proofs become cheap, repeatable SQL/model operations.

Targeted VM proof on `delta_india:ETH/USD:USD`, 30 days, horizon 8 bars, strict
20-trade/25 bps/PF 1.5 gates, core scalper scanners:

- `sats_5m_scalper_v1`
- `stealth_trail_bbp_v1`
- `human_trade_fingerprint_v1`

| Timeframe | Opportunities | OOS Model Trades | Raw Avg Net bps | Raw PF | Model Avg Net bps | Model PF | Improvement bps | Verdict |
|---|---:|---:|---:|---:|---:|---:|---:|---|
| 1m | 3,544 | 35 | -10.8229 | 0.2876 | -1.6824 | 0.8820 | +9.1406 | MODEL_IMPROVED |
| 5m | 336 | 1 | -1.8645 | 0.9002 | -9.5000 | n/a | -7.6355 | UNDER_SAMPLED |
| 15m | 268 | 3 | -17.5781 | 0.5682 | +85.6423 | 5.2354 | +103.2204 | UNDER_SAMPLED |
| 1h | 57 | 0 | n/a | n/a | n/a | n/a | n/a | UNDER_SAMPLED |
| 4h | 3 | 0 | n/a | n/a | n/a | n/a | n/a | UNDER_SAMPLED |

Important reading:

- 1m has enough OOS selections and the model materially improves the raw
  scanner baseline, but the selected set is still net negative and PF < 1.5.
- 5m is not currently proving the TradingView-style visual edge. It is almost
  flat raw, but the model selection is too sparse and worse OOS.
- 15m shows the strongest pulse, but only 3 OOS model trades. That is an
  interesting candidate for more data/cached mining, not a promotion.
- 1h/4h are not scalper verification horizons on a 30-day window; they are too
  sparse for this scanner family.

Bug found during 1m verification:

`stealth_trail_bbp_v1` could crash while precomputing exits when no valid
structural stop candidate existed below/above the reference price. The scanner
now falls back to conservative minimum-distance stop geometry instead of
raising `ValueError`, and a regression test covers the degenerate/flat ATR
case.

## Honest Reading

This PR proves the bot can learn enough from opportunity data to avoid some
worse scanner contexts. It does **not** prove a profitable scalper.

The evidence improved:

- Raw scanners: `-13.3998 bps`
- Model-routed subset: `-10.7997 bps`
- Loss reduction: `+2.6002 bps`

But it remains non-tradeable:

- Avg model net is still negative.
- PF is still below 1.5.
- OOS selected sample is below the strict 20-trade floor.
- The train selector looked much stronger than OOS, which is a clear
  non-stationarity warning.

## Next Required Build

`edge_model_v1` should now be used as the proof harness, not as a trading
signal. The next improvement should target the data itself:

1. Persist opportunity feature rows to a compact Parquet/DuckDB store.
2. Add richer ex-ante features from prepared strategy columns, not just reason
   strings and route geometry.
3. Train per-regime/per-symbol models with walk-forward folds, not one global
   30-day split.
4. Add drift guards so a train selector that collapses OOS is automatically
   downgraded.
5. Promote nothing until model-routed OOS clears avg net >= 25 bps, PF >= 1.5,
   and minimum sample floors on fresh data.
