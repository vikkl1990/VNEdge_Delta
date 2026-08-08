# Range Compression Breakout v1 — First Replay Result

Verdict: **rejected on selection; untouched 20% remains sealed**.

The preregistered contract and scanner implementation were committed before
the first observable historical result. The run used repaired, gap-free Delta
BTCUSD and ETHUSD 1m candles from 2025-01-01 through 2026-07-25. Selection
ended before the 2026-04-02 boundary with a 30-minute embargo.

## Selection evidence

| Metric | Result | Required | Pass |
|---|---:|---:|:---:|
| Trades | 197 | ≥100 | Yes |
| Frequency | 0.432/day | 0.10–4.00/day | Yes |
| Average gross | +0.965 bps | >14.8 bps cost | **No** |
| Average cost | 14.8 bps | modeled | — |
| Average net | −13.835 bps | ≥+3.0 bps | **No** |
| Total net | −2,725.525 bps | >0 | **No** |
| Profit factor | 0.312 | ≥1.20 | **No** |
| Win rate | 20.81% | diagnostic | — |
| False-signal rate | 79.19% | diagnostic | — |
| Missing minutes | 0 | 0 | Yes |
| Unresolved observations | 0 | 0 | Yes |

Exit attribution: 129 time stops, 59 stops, and only 9 targets. There were 99
long and 98 short observations, so failure was not a one-sided direction bug.

## Stability checks

| Slice | Trades | Net bps | Average net | PF |
|---|---:|---:|---:|---:|
| BTCUSD | 110 | −1,646.046 | −14.964 | 0.174 |
| ETHUSD | 87 | −1,079.479 | −12.408 | 0.453 |
| First chronological half | 90 | −1,105.566 | — | — |
| Second chronological half | 107 | −1,619.959 | — | — |

All five selection quarters were negative. Fourteen of fifteen active months
were negative; February 2026 was the lone positive month at +17.133 bps with
PF 1.076, still below the gate. Of 63 active weeks, 12 were positive. Of 165
active trading days, 33 were positive.

The calendar-complete daily table also includes every zero-trade selection day.
Daily, weekly, monthly, and quarterly CSVs are generated from the immutable
trade rows without evaluating the sealed tail.

## Decision

`range_compression_breakout_v1` is retired. No threshold relaxation, parameter
sweep, meta-labeling, or untouched evaluation is permitted on this contract.
The result indicates that the closed-candle breakout produces almost no gross
movement before exit (+0.965 bps/trade) and cannot approach 14.8 bps friction.

Safety remains unchanged: research only, `can_trade=false`,
`can_promote=false`, and no order route.
