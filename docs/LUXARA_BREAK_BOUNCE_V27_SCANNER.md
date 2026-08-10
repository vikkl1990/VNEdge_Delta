# Luxara Break & Bounce V27 Scanner

Status: research telemetry candidate, not paper/live eligible.

`luxara_break_bounce_v27_v1` is a VNEDGE-native adaptation of the supplied
Luxara Break & Bounce teaching overlay. The source chart builds a live setup
box from prior highs/lows, marks wick previews, confirms close/wick breakouts,
grades the setup from five checks, and displays entry/SL/TP levels.

VNEDGE keeps the causal box mechanics but changes the trading semantics:

- preview labels are telemetry only, never tradable intents,
- fixed chart-point targets are replaced with ATR/bps-aware structural exits,
- every confirmed breakout intent carries `expectedEdge` and
  `fillProbability` for the execution router,
- default signals are short-only, high-volume, tight-box breakouts.

## Current Default Research Gate

Defaults after VM mining:

- side: short only,
- trigger: close outside the prior setup box,
- grade: A or A+ (`>= 4/5`),
- volume impulse: `>= 2.5x` trailing average,
- setup box width: `<= 2.5 ATR`,
- breakout distance: `>= 12 bps`,
- expected net edge: `>= 80 bps`,
- maker-first route, taker only if the router says the move can pay costs.

These are anti-noise defaults, not promotion approval.

## VM Replay Proof on commit f7dc178

Data root: `/home/ubuntu/vnedge/data`

Container: `vnedge-research-loop:latest`

Route policy: research-only, 16-bar horizon on 15m candles, minimum 20 routed
samples, expected net edge floor 25 bps, PF floor 1.5.

### Raw / Broad Breakout Logic

The visual plan fires often, but broad confirmed box breakouts do not clear the
fee wall:

| Scope | Routed | Avg selected net | Verdict |
| --- | ---: | ---: | --- |
| Delta ETH 15m, 30d | 4 | -1.40 bps | Under-sampled negative |
| Delta ETH 5m, 30d | 1 | -63.84 bps | Under-sampled negative |
| 18-lane 15m universe, 30d | 97 | +4.61 bps | Below edge floor |
| 18-lane 15m universe, 90d | 299 | -14.98 bps | Negative |

### Strict Short / Tight-Box Pocket

The only mined pocket was short-side, high-volume, tight-box rejection:

| Scope | Routed | Avg selected net | PF / win | Verdict |
| --- | ---: | ---: | ---: | --- |
| Six pulse lanes, 90d | 22 | +75.96 bps | PF 4.04 / 72.7% | Interesting but sparse |
| Full 18-lane universe, 30d | 12 | +77.01 bps | under-sampled | No promotion |
| Full 18-lane universe, 90d | 48 | +13.05 bps | below edge floor | No promotion |

ETH shorts on Binance, Bybit, and Delta were strong but had only 3-4 routed
events per venue over 90d. SOL, BNB, BTC, and DOGE spillover weakened the
full-universe result. Raising the ex-ante edge floor to 120 bps left only 7
events; 180 bps left none.

## Decision

Keep `luxara_break_bounce_v27_v1` in the research/router sweep as a telemetry
and candidate-mining input. Do not promote it directly to paper. The next valid
step is not parameter relaxation; it is either:

- collect more 5m/15m history and retest the strict short/tight-box defaults,
- or pre-register an ETH-only untouched-window judgment if the operator wants
  to spend that data.

The honest lesson is narrow: Break & Bounce labels are not edge. The possible
edge is only in rare, high-participation downside breaks from compressed boxes.
