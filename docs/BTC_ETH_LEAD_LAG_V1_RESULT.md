# BTC to ETH Lead-Lag v1 — Frozen First-Replay Result

## Verdict

Rejected on the chronological selection window. The final 20% remained sealed:
no prediction, trade, metric, or threshold decision was computed on it.

The scanner and contract were frozen first in local commit `a77cbd9`. The replay
used contract SHA-256
`a3fdbc08968887ae7ba8860a4982c55234b11d02f9137cfb4dcdf0a36ebbc538`.
No parameter was changed after seeing these results.

## Selection evidence

The selected evidence window ran from 1 January 2025 through 1 April 2026. The
complete source window contained 820,687 exactly synchronized BTCUSD/ETHUSD
one-minute closes. The final split boundary was 2 April 2026 at 00:23 UTC,
preceded by the frozen 30-minute embargo.

| Metric | Frozen result |
|---|---:|
| Trades | 73 |
| Frequency | 0.160 per day |
| Average gross | -0.22 bps per trade |
| Average modeled cost | 14.80 bps per trade |
| Average net | -15.02 bps per trade |
| Total net | -1,096.45 bps |
| Profit factor | 0.334 |
| Win rate | 20.55% |
| False-signal rate | 79.45% |
| Long / short | 37 / 36 |
| Target / stop / time-stop | 13 / 53 / 7 |
| Missing synchronized minutes | 113 |

The first chronological half produced 34 trades and -515.24 bps. The second
half produced 39 trades and -581.20 bps. Gross expectancy was already negative
before fees, so maker assumptions or lower slippage cannot rescue this frozen
trade set.

## Frozen selection gates

| Gate | Threshold | Result |
|---|---|---|
| Data quality | Zero missing synchronized minutes | Fail: 113 missing |
| Minimum trades | At least 100 | Fail: 73 |
| Trades per half | At least 35 in each half | Fail: 34 / 39 |
| Average net | At least +3.0 bps | Fail: -15.02 bps |
| Profit factor | At least 1.20 | Fail: 0.334 |
| Gross clears cost | Average gross greater than cost | Fail |
| Frequency | 0.10 to 4.0 trades/day | Pass: 0.160 |
| Temporal consistency | Both halves positive | Fail: both negative |

Because the combined selection gate failed, `untouched.status` is `sealed`,
`predictions_computed` is false, and `trades_computed` is false.

## Monthly selection view

Small positive months are descriptive only and cannot override the frozen
selection gate.

| Month | Trades | Net bps | Avg bps | PF |
|---|---:|---:|---:|---:|
| 2025-01 | 9 | -119.82 | -13.31 | 0.367 |
| 2025-02 | 6 | -122.20 | -20.37 | 0.257 |
| 2025-03 | 8 | -185.44 | -23.18 | 0.166 |
| 2025-04 | 4 | +29.34 | +7.33 | 1.471 |
| 2025-05 | 3 | -68.80 | -22.93 | 0.000 |
| 2025-06 | 2 | +5.24 | +2.62 | 1.165 |
| 2025-07 | 2 | -53.57 | -26.79 | 0.000 |
| 2025-08 | 1 | -48.50 | -48.50 | 0.000 |
| 2025-09 | 2 | +36.89 | +18.45 | 98.181 |
| 2025-10 | 4 | -93.28 | -23.32 | 0.000 |
| 2025-11 | 5 | -89.07 | -17.81 | 0.295 |
| 2025-12 | 7 | -26.43 | -3.78 | 0.753 |
| 2026-01 | 4 | -113.40 | -28.35 | 0.000 |
| 2026-02 | 8 | -182.82 | -22.85 | 0.195 |
| 2026-03 | 8 | -64.59 | -8.07 | 0.535 |

## Quarterly selection view

| Quarter | Trades | Net bps | Avg bps | PF |
|---|---:|---:|---:|---:|
| 2025-Q1 | 23 | -427.46 | -18.59 | 0.258 |
| 2025-Q2 | 9 | -34.21 | -3.80 | 0.790 |
| 2025-Q3 | 5 | -65.18 | -13.04 | 0.364 |
| 2025-Q4 | 16 | -208.78 | -13.05 | 0.361 |
| 2026-Q1 | 20 | -360.81 | -18.04 | 0.247 |

## Decision

`btc_eth_lead_lag_v1` is a rejected research artifact. Do not tune it on this
observed window, do not open the untouched tail, and do not route it to paper or
live execution. A materially different v2 requires a new hypothesis, contract,
version, and sealed evaluation.
