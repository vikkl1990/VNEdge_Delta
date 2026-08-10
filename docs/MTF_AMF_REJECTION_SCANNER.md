# MTF/AMF rejection scanner

## Purpose

`mtf_amf_rejection_scanner_v1` is a causal, research-only scanner distilled
from two supplied Pine indicators:

- Trinity Multi-Timeframe S/R Levels (EMA34TRADER)
- Adaptive Momentum Fusion v1.2.1 (WillyAlgoTrader)

It does not copy either visual indicator. It ports the tested mechanism:
completed multi-timeframe range confluence, candle rejection, Efficiency-mode
adaptive momentum alignment, and a ranging-regime filter.

The scanner is deliberately not in `strategy_registry.py`. It cannot create a
`Signal`, order intent, stop, target, paper trade, or live trade.

## Evidence-locked configuration

| Component | Locked value |
| --- | --- |
| Signal chart | 1 hour |
| Level timeframes | 1 hour + completed 4 hour |
| Range lookback | 20 bars on each timeframe |
| Level confluence | distance <= 0.10 ATR(14) |
| Candle setup | wick through level, close back inside |
| AMF engine | Efficiency, fast 8, slow 21, signal 7 |
| AMF direction | histogram > 0 long; histogram < 0 short |
| Regime | AMF regime < 0.50 (ranging) |
| Duplicate suppression | 62 one-hour bars per symbol |
| Observation horizons | 15, 40, and 62 one-hour bars |
| Observation fill model | next 1h open to fixed-horizon close |
| Cost lens | 12.5 bps round-trip assumption |

The configuration rejects other chart/higher timeframes. Testing many
timeframes was useful during discovery, but allowing post-selection drift in
the deployed scanner would turn the selected result into another optimizer.

## Causality rules

1. The current 1h bar is excluded from its own 20-bar high and low.
2. A 4h candle stamped at `08:00` is not available until `12:00`.
3. The signal is observed only after the 1h rejection candle closes.
4. AMF recursion uses current and prior closes only.
5. Appending future candles cannot rewrite past features or alerts; this is
   covered by truncation-invariance tests.

This is stricter than `request.security(..., lookahead_off)` by itself. A
forming higher-timeframe candle can still move its range during the candle, so
the original value must be delayed until completion.

## Research result and limitations

The selected non-overlapping candidate survived the original chronological
split, but the sample was small and concentrated:

| Slice | Signals | Average net bps/observation | Profit factor |
| --- | ---: | ---: | ---: |
| Train | 68 | +90.96 | 1.50 |
| Validation | 27 | +225.65 | 3.43 |
| Audit | 22 | +152.87 | 3.00 |

The audit rate was only about 0.19 observations per day and most audit profit
came from SOL. BTC and ETH market-specific lower confidence bounds were
negative. This does **not** support a claim of 10-15 profitable trades per day.

A separate 648-combination target/stop/timeout search failed audit at about
-1.18 bps per trade and PF 0.98. For that reason the scanner implements no
bracket exit and reports fixed-horizon observations rather than executable
P&L. Its JSON policy is always:

```json
{
  "research_only": true,
  "can_trade": false,
  "can_promote": false,
  "registered_strategy": false,
  "order_route_present": false,
  "bracket_exit_status": "failed_audit_not_implemented",
  "observation_fill_model": "next_open_to_fixed_horizon_close"
}
```

### Implementation replay smoke check

The implemented scanner was run against the stored Delta India 15m candles,
resampled into complete 1h/4h bars from 2025-01-01 through 2026-08-03. This is
already-seen discovery data, so it is an implementation/parity check—not new
validation evidence:

| Market | Non-overlapping alerts | 62h after-cost bps/observation | 62h PF |
| --- | ---: | ---: | ---: |
| BTCUSD | 42 | +66.81 | 1.56 |
| ETHUSD | 33 | +81.11 | 1.43 |
| SOLUSD | 43 | +197.13 | 2.51 |

Across all three markets this is only about 0.61 alerts/day in total. The
result supports the scanner implementation and the earlier conclusion that the
mechanism is a sparse swing observation, not a 10-15-trade/day engine.

## Run locally

Provide canonical 1h and 4h CSV or Parquet files containing `timestamp`,
`open`, `high`, `low`, and `close`:

```bash
python -m vnedge.research.mtf_amf_rejection_scanner \
  --one-hour data/ETHUSD-1h.parquet \
  --four-hour data/ETHUSD-4h.parquet \
  --symbol ETHUSD
```

The default report is written atomically to
`research/live_research/mtf_amf_rejection_scanner_latest.json`.

Forward alert journaling, 1h/4h/12h/24h MFE/MAE labels, expanded-market
validation, and the dashboard evidence contract are documented in
`docs/SCANNER_FORWARD_EVIDENCE.md`. L2 imbalance and flow are context fields
only and never alter this scanner's signal or execution state.

For a continuously refreshed, credential-free Delta India research snapshot:

```bash
python -m vnedge.research.mtf_amf_rejection_scanner \
  --delta-live \
  --symbols BTCUSD,ETHUSD,SOLUSD \
  --interval-seconds 300
```

This mode downloads only public candles, removes the still-forming 1h/4h
candles, and rewrites the same latest JSON atomically. It remains observation
only and has no exchange order client.

## Promotion path

The next legitimate step is shadow observation on a pre-registered untouched
period and multiple markets. Promotion requires stable market-level evidence,
an exit model that passes its own untouched audit, execution-cost replay, and
human approval. Scanner activity alone is not promotion evidence.
