# HTF Structure Break v2 — Frozen Selection Result

Run date: 10 August 2026

Contract: `configs/research/htf_structure_break_v2.yaml`

Contract SHA-256: `4dd25ac8bb43f2e2f10e324d8b6057e20f5214df01259fa10b7c98fbcf638442`

Decision: **FAIL / RETIRE v2**

Untouched window: **SEALED**

## What changed from v1

The failed v1 contract was not overwritten. Version 2 is a separate frozen
hypothesis implementing the supplied higher-timeframe specification:

- 4h EMA20/EMA50 direction;
- confirmed 1h swings with five left and five right bars;
- bias-aligned BOS only;
- nearest already-confirmed 1h/4h structural target;
- stop beyond the broken swing by 0.10 x 1h ATR(14);
- target at least five times the 14.8 bps baseline round-trip cost;
- one pending/open observation per market;
- absolute stop/target geometry checked again at the entry proxy;
- 12-hour vertical barrier;
- stop-first resolution on same-bar ambiguity;
- causal funding-rate samples restricted to 8-hour exchange boundaries.

The proposal's `2.0 bps` slippage per leg did not agree with its stated 14.8
bps round trip. Version 2 froze `1.5 bps` per leg because
`2 x 5 x 1.18 + 2 x 1.5 = 14.8 bps`. Two bps per leg would be 15.8 bps.

## Selection result

| Metric | Result | Gate |
|---|---:|---:|
| Trades | 100 | at least 80: pass |
| Frequency | 0.220/day across both markets | pass |
| Average gross | +4.51 bps/trade | must exceed total cost: fail |
| Baseline fee + slippage | 14.80 bps/trade | configured |
| Average funding | +0.21 bps/trade | included |
| Average total cost | 15.01 bps/trade | included |
| Average net | **-10.50 bps/trade** | at least +3: fail |
| Total net | **-1,049.96 bps** | fail |
| Profit factor | **0.843** | at least 1.20: fail |
| Win rate | 34.0% | evidence only |
| False-signal rate | 66.0% | evidence only |
| Exits | 65 stop / 32 target / 3 time-stop | evidence only |

## Market and chronology stability

| Slice | Trades | Average net / total net | Profit factor |
|---|---:|---:|---:|
| BTCUSD | 27 | -35.43 bps/trade | 0.435 |
| ETHUSD | 73 | -1.28 bps/trade | 0.981 |
| First chronological half | 52 | +493.20 bps total | — |
| Second chronological half | 48 | -1,543.16 bps total | — |

The result is not robust. ETH approached fee-adjusted breakeven, but BTC was
strongly negative and the second half reversed the first-half gain. This is
exactly the type of instability the frozen gates are designed to reject.

## Funnel

- 21,624 eligible 1h evaluations after aggregation.
- 20,600 had no new bias-aligned BOS.
- 504 BOS candidates failed the five-times-cost target gate.
- 25 lacked a causal structural target.
- 103 candidates were emitted.
- Three failed geometry when repriced at the entry proxy.
- 100 observations resolved.

## Latency limitation found during review

A 1h candle close and the following 1h interval open share the same exchange
boundary. A scanner that receives the close one to thirty seconds later cannot
literally fill at that already-passed open. The replay uses that open as a
standard candle-research proxy and charges 1.5 bps slippage per leg, but it is
not executable fill proof. A future version must either:

1. enter at the first fully observable post-decision price (for candle-only
   research, the following 1m open), or
2. use event replay and latency-aware fills after the exchange close event.

This limitation cannot rescue the result: the current proxy is generally more
favorable than a delayed executable fill, and the hypothesis already fails.

## Disposition

- `htf_structure_break_v2`: **retired**
- Untouched data: **not loaded or scored**
- Paper trading: **blocked**
- Live trading: **blocked**
- `can_trade`: **false**
- `can_promote`: **false**
- No threshold or market-specific tuning is permitted under this version.

Machine-readable evidence:
`research/live_research/htf_structure_break_v2_latest.json`.
