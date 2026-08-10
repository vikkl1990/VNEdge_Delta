# Delta Funding Audit v1 — Final Result

Run date: 8 August 2026  
Contract: `configs/research/delta_funding_audit_v1.yaml`  
Decision: **NO-GO**  
Untouched window: **SEALED**

## Scope

This was a selection-only audit of Delta India's native `FUNDING:BTCUSD` and
`FUNDING:ETHUSD` hourly candle history from 1 January 2025 through 2 April
2026. It tested historical integrity first, then measured price behavior after
causal funding extremes. It did not create a scanner, score the sealed tail,
or enable any trading route.

## Integrity findings and repairs

The audit found two causality/data-retrieval defects before the economic test:

1. Delta returns the currently forming hourly funding candle. Its API `time`
   is the interval start, while the final `close` is only usable at
   `time + 1 hour`. The loader previously exposed that changing close at the
   interval start. It now excludes forming rows and timestamps completed
   values at their conservative availability time.
2. Some 1,500-hour API requests silently returned only 1,497 rows. This made
   the old pagination path lose seven hours at repeated page boundaries. The
   fetcher now uses 1,000-hour pages and deduplicates page overlaps.

After those repairs, the frozen audit passed for both markets:

| Integrity metric | BTCUSD | ETHUSD |
|---|---:|---:|
| Expected settled prints | 10,944 | 10,944 |
| Settled unique prints | 10,944 | 10,944 |
| Missing prints | 0 | 0 |
| Malformed rows | 0 | 0 |
| Conflicting duplicate timestamps | 0 | 0 |
| Benign page-overlap timestamps | 10 | 10 |
| Forming rows excluded | 1 | 1 |
| Raw unit | percent | percent |
| Canonical unit | fraction (`raw / 100`) | fraction (`raw / 100`) |

No missing funding value was forward-filled. Event timestamps use only the
settled availability time.

## Frozen diagnostic

- Extreme: absolute causal z-score at least 2.0, calculated from the preceding
  240 settled prints and excluding the current print.
- Event: crossing into the extreme region, followed by a 24-hour cooldown.
- Entry: first 1-minute open at or after the funding close becomes available.
- Directions tested: continuation and reversal.
- Primary horizon: 8 hours.
- Secondary horizons: 1, 4, 12 and 24 hours.
- Round-trip cost: 14.8 bps.
- Events: 174 total — 103 BTCUSD and 71 ETHUSD.

## Primary 8-hour result

| Orientation | Events | Avg gross | Avg net | Profit factor | First-half net/trade | Second-half net/trade |
|---|---:|---:|---:|---:|---:|---:|
| Continuation | 174 | +6.01 bps | **-8.79 bps** | **0.876** | +4.12 bps | -21.69 bps |
| Reversal | 174 | -6.01 bps | **-20.81 bps** | **0.734** | -33.72 bps | -7.91 bps |

Market consistency also failed:

| Orientation | BTCUSD avg net / PF | ETHUSD avg net / PF |
|---|---:|---:|
| Continuation | -19.96 bps / 0.675 | +7.42 bps / 1.088 |
| Reversal | -9.64 bps / 0.833 | -37.02 bps / 0.657 |

Both orientations fail positive expectancy, required profit factor, market
consistency, and chronological stability. The diagnostic therefore does not
permit preregistration of `funding_squeeze_reversal_v1`.

## Secondary horizons

| Orientation | Horizon | Avg gross | Avg net | Profit factor |
|---|---:|---:|---:|---:|
| Continuation | 1h | -4.85 bps | -19.65 bps | 0.403 |
| Continuation | 4h | -3.96 bps | -18.76 bps | 0.669 |
| Continuation | 12h | +10.19 bps | -4.61 bps | 0.947 |
| Continuation | 24h | +30.61 bps | +15.81 bps | 1.154 |
| Reversal | 1h | +4.85 bps | -9.95 bps | 0.646 |
| Reversal | 4h | +3.96 bps | -10.84 bps | 0.790 |
| Reversal | 12h | -10.19 bps | -24.99 bps | 0.743 |
| Reversal | 24h | -30.61 bps | -45.41 bps | 0.666 |

The 24-hour continuation row is an exploratory observation, not a pass. It is
market-dependent: BTCUSD averaged +36.53 bps with PF 1.543, while ETHUSD
averaged -13.96 bps with PF 0.909. It also was not the preregistered primary
horizon. It may motivate a separately preregistered, BTC-only multi-hour
continuation hypothesis later, but it cannot be promoted or used to reopen
this contract.

## Safety disposition

- `funding_squeeze_reversal_v1`: **not preregistered**
- Paper trading: **blocked**
- Live trading: **blocked**
- `can_trade`: **false**
- `can_promote`: **false**
- Sealed window 2 April–25 July 2026: no funding values read, no price values
  read, no predictions computed, never opened

Machine-readable evidence is stored in
`research/live_research/delta_funding_audit_v1_latest.json`; causal event paths
are stored in `research/live_research/delta_funding_audit_v1_events.parquet`.
