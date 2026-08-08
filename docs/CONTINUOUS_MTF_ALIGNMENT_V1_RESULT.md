# Continuous Multi-Timeframe Alignment v1 — First Result

Run date: 8 August 2026  
Contract: `configs/research/continuous_mtf_alignment_v1.yaml`  
Selection decision: **REJECTED**  
Untouched tail: **SEALED**

## What was implemented

`continuous_mtf_alignment_v1` is a persistent, event-driven state machine—not
five independent scanners. Every proven 1m, 5m, 15m, 1h and 4h close updates
one state object for the market. Evaluation happens only after every candle
sharing the same close timestamp has been applied.

The implemented lifecycle is:

```text
4h stable EMA/ADX bias + confirmed swing levels
  -> 1h aligned structure / BOS / CHOCH
  -> one active mechanical 15m pullback or breakout zone
  -> 5m directional body + previous-bar break + volume confirmation
  -> 1m three-bar structure break + volume trigger
  -> next 1m open geometry
  -> reject unless structural target >= 4x cost and reward/risk >= 1.5
```

Higher-timeframe neutralization, opposition, a breached 15m zone, expiry, or a
source-data gap cancels the relevant lower state immediately. A target is a
pre-existing 1h extreme or confirmed 4h swing; the engine is forbidden from
raising the target to satisfy its fee floor.

The implementation remains a locked research component. It is not connected
to an order route and was not activated in the live shadow scanner after the
failed selection result.

## Selection protocol

- Markets: BTCUSD and ETHUSD
- Source: causally aggregated, gap-free 1m candles
- Selection: 1 January 2025 through 1 April 2026 20:01 UTC
- Four-hour embargo before the sealed boundary to resolve open observations
- Entry: next 1m open
- Intrabar ambiguity: stop first
- Time stop: four hours
- Round-trip cost: 14.8 bps
- No regime overlay, L2 trigger, funding input, meta-model, or parameter search

## Result

| Metric | Value |
|---|---:|
| State-qualified intents | 421 |
| Accepted geometries / completed trades | 118 |
| Trades per day | 0.259 |
| Average structural target | 197.39 bps |
| Average gross return | +2.10 bps |
| Average modeled cost | 14.80 bps |
| Average net return | **-12.70 bps** |
| Total net | **-1,499.04 bps** |
| Profit factor | **0.714** |
| Win rate | 30.51% |
| False-signal rate | 69.49% |
| Positive markets | 0 of 2 |
| Missing source minutes | 0 |

### Market attribution

| Market | Trades | Avg net | Profit factor |
|---|---:|---:|---:|
| BTCUSD | 46 | -20.89 bps | 0.497 |
| ETHUSD | 72 | -7.47 bps | 0.839 |

### Setup attribution

| Setup | Trades | Avg gross | Avg net | Profit factor |
|---|---:|---:|---:|---:|
| 15m pullback | 110 | +3.20 bps | -11.60 bps | 0.742 |
| 15m breakout | 8 | -13.05 bps | -27.85 bps | 0.246 |

### Geometry rejects

| Reason | Intents |
|---|---:|
| Reward/risk below 1.5 | 196 |
| Invalid after next-open gap | 94 |
| Structural target below 4x cost | 13 |

The accepted trades were not failing because targets were too small: their
median target was 137.00 bps, median reward/risk was 2.62, and median target
was 9.26 times modeled costs. They failed because entries did not convert that
available structure into realized movement: average gross was only +2.10 bps.

## Comparison with the retired hierarchical pullback

| Version | Trades | Avg net | PF | False signals |
|---|---:|---:|---:|---:|
| Retired snapshot hierarchy | 506 | -15.03 bps | 0.319 | 79.25% |
| Continuous shared state | 118 | -12.70 bps | 0.714 | 69.49% |

Continuous awareness materially reduced noise and improved relative ranking,
but it did not cross costs or produce a profitable market. Coherence is useful
engineering; it is not standalone alpha.

## Gate decision and safety

Passed:

- Minimum sample size
- Data quality
- Frequency/concentration control
- False-signal ceiling, narrowly

Failed:

- Gross expectancy above costs
- Positive net expectancy
- Profit factor at least 1.15
- At least one positive market

Therefore v1 is rejected, the untouched 2 April–25 July 2026 tail remains
unopened, and the component is not eligible for paper or live activation.

Machine-readable evidence and exactly-once intent records are stored in
`research/live_research/continuous_mtf_alignment_v1_latest.json`.
