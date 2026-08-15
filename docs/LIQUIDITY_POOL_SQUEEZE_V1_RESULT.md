# Liquidity Pool × Squeeze clean-room v1 — selection result

Verdict: **rejected on selection; sealed tail remains unopened**.

The frozen BTCUSD/ETHUSD run used completed 15m decisions causally aggregated
from gap-free 1m candles between 2025-01-01 and 2026-04-02. The final decision
was 2026-04-01 23:45 UTC; no candle at or after the sealed boundary was loaded
into the decision engine.

## Final frozen selection results

Favorable movement from a candle that touched the stop is not credited because
OHLC cannot prove that the favorable excursion occurred before the stop.
Matched controls also exclude every raw pool-sweep candle, including sweeps
that did not pass the current variant's gates.

| Variant | Trades | Avg gross | Avg net | PF | Control MFE uplift | Control gate |
|---|---:|---:|---:|---:|---:|:---:|
| Pool sweep standalone | 1,699 | −0.73 bps | −15.53 bps | 0.49 | +1.60 bps | Pass |
| Pool × Squeeze full | 294 | +0.62 bps | −14.19 bps | 0.60 | −2.04 bps | **Fail** |
| Remove squeeze | 588 | +1.05 bps | −13.75 bps | 0.62 | +4.13 bps | Pass |
| Remove volume | 398 | −0.77 bps | −15.57 bps | 0.55 | −0.73 bps | **Fail** |
| Remove target room | 581 | −1.32 bps | −16.12 bps | 0.48 | −1.64 bps | **Fail** |

The full interaction lost on BTCUSD and ETHUSD and on both long and short
sides. It produced 210 stops, 35 targets and 49 time stops. Its least-negative
cell was short signals at −6.51 bps/trade and PF 0.81—still not an edge. The
squeeze gate did not select abnormal movement: full-interaction events had
lower MFE than earlier same-symbol/session/volatility controls.

## Interpretation

- The standalone sweep has small positive abnormal-movement uplift but no
  directional gross edge. It cannot pay the 14.8 bps route cost.
- Opposing-pool target room improves excursion size, but not directional
  capture.
- The squeeze interaction makes matched-control selection worse. It appears to
  identify ordinary volatile conditions rather than an independently abnormal
  pool event.
- Because the full interaction failed both selection economics and matched
  controls, exit optimization is locked. Testing exit permutations would be
  post-selection overfitting.

No strategy registry lane was created. AMF v3 remains a separate 15/60 swing
evidence stream and received no observations from this study. `can_trade` and
`can_promote` remain false.
