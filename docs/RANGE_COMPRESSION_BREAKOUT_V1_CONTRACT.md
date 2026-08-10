# Range Compression Breakout v1 — Preregistered Contract

Status: frozen before first historical scanner replay. Research only; no order
route, paper route, or promotion authority.

## Hypothesis

After an extended, causally observable contraction in five-minute Bollinger
bandwidth, the first decisive close outside the compression range may continue
far enough to overcome Delta India taker fees, GST, and slippage when it is
accompanied by volume expansion, true-range expansion, and a recent volatility
or return CUSUM shift.

This is a standalone OHLCV hypothesis for BTCUSD and ETHUSD. It does not reuse
Momentum Burst, Imbalance Fade, hierarchical pullback, BTC→ETH lead-lag,
funding, L2, CVD, or a meta-model.

## Inputs and causality

- Delta BTCUSD and ETHUSD proven-closed 1m OHLCV, causally aggregated to 5m.
- Decision only at a completed 5m close; entry only at the next 1m open.
- Full window: 2025-01-01 00:00 UTC through 2026-07-25 00:00 UTC.
- Every expected source minute must exist. No interpolation or forward fill.
- A missing minute resets aggregation, compression, CUSUM, cooldown, pending
  entry, and open research-observation state and fails the quality gate.

## Compression definition

For each completed 5m bar, Bollinger width is `4 × population standard
deviation / mean` over 20 closes. Its percentile rank uses at most the 200
width observations already available at that close.

Before a decision bar may break out:

1. At least 6 of the preceding 12 completed 5m bars had width percentile at or
   below 20% using only information available at each respective close.
2. The immediately preceding 5m bar was compressed by the same definition.
3. The breakout range is the high and low of those preceding 12 bars; the
   decision bar itself is excluded.

## Breakout decision

A long requires the decision close at least 2 bps above the prior range high;
a short requires the mirrored close below the prior range low. Both directions
also require:

- body/range ratio at least 0.60;
- decision volume at least 1.50× the median volume of the preceding 20 bars;
- decision true range at least 1.20× ATR(14), where ATR uses only prior bars;
- frozen causal CUSUM shift on 5m return or log true range no more than 3 bars
  ago (threshold 8.0z, drift 0.5z, 200-bar baseline, 50-bar minimum history,
  6-bar detector cooldown).

The setup identity is symbol, prior compression-range close, and direction. It
can emit once. A 360-minute per-symbol cooldown follows an emitted observation.
Only one pending or open observation is allowed per symbol.

## Entry and exits

- Entry: next 1m open after the completed decision bar.
- Structural stop: decision-bar opposite extreme plus a 2 bps buffer.
- Stop floor: 15 bps. If the raw structural stop exceeds 60 bps, reject the
  candidate; do not clamp an invalid wide stop into eligibility.
- Target: maximum of 2.50R and 3.50× modeled round-trip cost.
- Vertical barrier: 1,800 seconds.
- If stop and target touch within one 1m candle, stop wins.
- Both legs are modeled as taker. Each leg includes 5 bps pre-tax fee, 18% GST
  on the fee, and 1.5 bps slippage. Round-trip modeled cost is 14.8 bps. No
  Scalper Offer, DETO discount, maker fill, or rebate is assumed.

Probability 0.75 and confidence 0.65 are uncalibrated structural placeholders
needed by the shared candidate contract. They have no promotion authority.

## Chronological selection and sealed tail

The first 80% of time is the selection window. Decisions stop 30 minutes before
the split, so no vertical barrier crosses into the final 20%. The untouched
tail remains computationally sealed unless every selection check passes:

- zero missing source minutes and zero unresolved observations;
- at least 100 trades total, 35 in each chronological half, and 30 per market;
- average net at least +3 bps per trade after modeled costs;
- profit factor at least 1.20;
- average gross return greater than average modeled cost;
- combined frequency between 0.10 and 4.00 trades per day;
- positive net in both chronological halves and both markets.

Passing selection permits exactly one untouched evaluation; it does not permit
paper or live trading. Parameters may not be changed on this historical window.
Failure retires v1 without opening the tail or lowering any gate.

The machine-readable authority is
`configs/research/range_compression_breakout_v1.yaml`.
