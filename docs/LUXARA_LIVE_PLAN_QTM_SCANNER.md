# Luxara Live Plan QTM Scanner

Status: research candidate, not paper/live eligible yet.

`luxara_live_plan_qtm_v1` is a VNEDGE-native adaptation of the supplied
Luxara Live Plan / QTM overlay.  The visual script behaves like an operator
plan: ATR trail flips create BUY/SELL labels, while EMA, RSI, candle color, and
support/resistance midline produce a 5-point grade and entry/SL/TP plan.

VNEDGE keeps the causal plan features but makes three trading changes:

- fixed chart-point TP/SL values are replaced with ATR/bps-aware stop and
  target geometry,
- every intent carries `expectedEdge` and `fillProbability` for the execution
  router,
- default execution signals are strict, asymmetric, high-room longs only.

The short side and grade-only filters were negative in replay; mirroring every
TradingView label would add noise to the bot.

## Current Default Research Gate

Defaults after VM mining:

- side: long only,
- signal mode: QTM trend flip,
- minimum grade: B / 3 of 5,
- minimum volume ratio: 1.5x trailing average,
- minimum expected edge: 120 bps,
- minimum room to liquidity: 150 bps,
- maker-first route, taker only if the router says the move can pay costs.

These are research defaults, not promotion approval.

## VM Replay Proof on commit 2e36ebb

Data root: `/home/ubuntu/vnedge/data`

Container: `vnedge-research-loop:latest`

Route policy: research-only, 16-bar horizon on 15m candles, minimum 20 routed
samples, expected net edge floor 25 bps, PF floor 1.5.

### Raw Visual Plan

The unfiltered live plan fires continuously but does not clear costs.

| Scope | Routed | Avg selected net | PF | Verdict |
| --- | ---: | ---: | ---: | --- |
| Delta ETH 5m, 30d | 232 | -9.18 bps | 0.59 | Negative |
| 18-lane 15m universe, 30d | 4,819 | -3.39 bps | 0.90 | Negative |
| 18-lane 15m universe, 90d | 14,372 | -6.78 bps | 0.79 | Negative |

### Strict High-Room Long Defaults

The strict version is materially better.

| Scope | Routed | Avg selected net | PF | Win rate | Verdict |
| --- | ---: | ---: | ---: | ---: | --- |
| 18-lane 15m universe, 30d | 96 | +55.63 bps | 2.96 | 62.5% | Candidate aggregate |
| 18-lane 15m universe, 90d | 248 | +27.30 bps | 1.61 | 50.8% | Candidate aggregate |
| Bybit DOGE 15m, 90d | 27 | +28.02 bps | 1.55 | 48.1% | Maker-edge lane |

Important caveat: the strict gate was derived from the same research data.
This is not an untouched judgment pass.  Treat it as a pre-registration input:
the next step is a fresh untouched-window judgment with these exact defaults.

## Decision

Keep `luxara_live_plan_qtm_v1` in research/router sweeps as a serious
candidate.  Do not promote it directly to paper yet.

The valid lesson is not "Luxara labels win."  The valid lesson is narrower:
QTM flips become interesting only when the plan has substantial room to
liquidity, volume participation, and enough expected edge to pay maker/taker
costs.  Grade by itself is not predictive.
