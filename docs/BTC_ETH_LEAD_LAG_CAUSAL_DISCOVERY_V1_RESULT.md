# BTC–ETH Lead-Lag Causal Discovery v1 — Result

## Verdict

Rejected. No tested BTC→ETH timeframe/lag passed the frozen advancement gate,
so this study does not authorize a lead-lag v2 scanner contract or backtest.
The old `btc_eth_lead_lag_v1` final 20% remained sealed and uncomputed.

The study contract and statistical design were frozen in local commit
`2883fc4`, with configuration SHA-256
`b9ad9a850e5a2f68fa227fc5e29309aee09756b061ef497ef78b318cb96b033f`.
Commit `20ffe8d` corrected an existing candle-loader call before any data was
read. Commit `c500402` replaced a non-standard JSON `Infinity` with `null`; the
identical rerun did not change any statistic or verdict.

## Data and method

| Item | Result |
|---|---:|
| Selection-only window | 2025-01-01 through 2026-04-01 23:53 UTC |
| 1m synchronized return observations | 656,517 |
| 5m complete-bucket return observations | 131,300 |
| Gap-separated segments | 3 per timeframe |
| Rolling windows | 13 × 90 days, stepped 30 days |
| Within-window chronology | 70% train / 30% test |
| Tests per rolling window | 16 |
| Multiple-test correction | Benjamini–Hochberg |

## Primary rolling BTC→ETH evidence

Significance is the fraction of rolling windows with corrected p ≤ 0.01. OOS
means the median reduction in restricted-model test MSE after adding BTC lags;
negative values mean prediction became worse.

| Timeframe | Max lag | Significant windows | Positive OOS windows | Median OOS MSE change | Median sign uplift |
|---|---:|---:|---:|---:|---:|
| 1m | 1 | 84.62% | 46.15% | -0.0127% | -0.401 pp |
| 1m | 2 | 92.31% | 46.15% | -0.0209% | -0.273 pp |
| 1m | 3 | 92.31% | 46.15% | -0.0254% | -0.412 pp |
| 1m | 6 | 100.00% | 38.46% | -0.0400% | -0.286 pp |
| 5m | 1 | 38.46% | 46.15% | -0.0190% | +0.039 pp |
| 5m | 2 | 53.85% | 38.46% | -0.0334% | -0.334 pp |
| 5m | 3 | 46.15% | 38.46% | -0.0674% | -0.103 pp |
| 5m | 6 | 61.54% | 30.77% | -0.1037% | -0.064 pp |

Every BTC→ETH cell had negative median OOS MSE improvement. Seven of eight also
worsened median sign accuracy. The 1m tests often produced extremely small
p-values and positive in-sample incremental R², but the rolling test periods
show that this did not generalize.

## Reverse-direction diagnostic

ETH→BTC was relatively stronger at 1m: median OOS MSE improvement ranged from
+0.0369% to +0.0580%, with positive median sign uplift. However, it improved in
only 53.85% of rolling windows, below the frozen 60% stability requirement, and
the study explicitly required BTC→ETH for advancement. No reverse cell passed.

The descriptive full-period split was not allowed to override rolling evidence.
For example, full-period BTC→ETH 1m lag 1 showed a tiny +0.0011% OOS MSE change,
while its primary rolling median was -0.0127%.

## Gate decision

- Valid-window count passed for every cell.
- Some significance gates passed because of the large sample.
- Every BTC→ETH cell failed positive-OOS stability and median OOS improvement.
- Seven BTC→ETH cells failed median sign uplift.
- No eligible advancement cell exists.
- `scanner_backtest_authorized = false`.
- Sealed-tail access, predictions, and trades are all false.

## Interpretation

The existing candle data contains weak linear dependence, but not stable
BTC-leading-ETH predictive value. Statistical significance without rolling OOS
improvement is not an edge. Do not build or tune a lead-lag v2 from this study.
The next hypothesis in the locked sequence remains range-compression breakout.
