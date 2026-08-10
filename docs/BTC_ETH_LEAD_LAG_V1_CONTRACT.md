# BTC → ETH Lead-Lag v1 — Preregistered Contract

Status: frozen before first scanner replay. Research only; no order route.

## Hypothesis

A sufficiently large, volume-supported BTC move can precede a smaller ETH move
on synchronized closed one-minute candles. If ETH has not already followed and
then prints its own directional one-minute break, entering ETH at the next
one-minute open may have positive standalone expectancy after configured costs.

This is an ETH-only hypothesis. BTC is information, never the traded leg.

## Inputs and causality

- Delta BTCUSD and ETHUSD closed 1m OHLCV.
- Exact UTC close-timestamp intersection only.
- Five-bar BTC and ETH returns use the current close and a prior closed close.
- Volume z-scores use only the 20 bars preceding the decision bar.
- No funding, L2, CVD, future candle, incomplete candle, interpolation, or
  forward-filled price is allowed.
- Any missing minute clears warmup, pending entry, open observation, and
  scanner cooldown state. Missing data fails the data-quality gate.

## Decision tree

1. BTC absolute five-minute return is at least 20 bps.
2. BTC last-minute return is at least 5 bps in the same direction.
3. BTC decision-bar volume z-score is at least 1.0.
4. ETH absolute five-minute return is no more than 12 bps.
5. ETH directional follow-through is no more than 40% of BTC's move.
6. Directional BTC-minus-ETH lead gap is at least 12 bps.
7. ETH closes through the previous ETH 1m high/low in the BTC direction.
8. ETH trigger body ratio is at least 0.50 and volume z-score at least 0.50.

The impulse identity is BTC close timestamp plus direction. It can emit once.
A 240-minute cooldown follows an emitted observation. The replay permits only
one pending or open ETH observation at a time.

## Entry and exits

- Decision: synchronized closed BTC/ETH bar.
- Entry: next synchronized ETH 1m open.
- Stop: 0.80 × ETH 1m ATR(14), clamped to 12–40 bps.
- Target: maximum of 2.50R and 3.50 × modeled round-trip cost.
- Vertical barrier: 1,680 seconds.
- If stop and target touch in one bar, stop wins.
- Entry is modeled as taker. Standard fees, GST, and 1.5 bps slippage per leg
  apply. Scalper Offer and DETO are not assumed.

Probability 0.75 and confidence 0.65 are structural placeholders needed by the
shared candidate contract. They are explicitly uncalibrated, cannot justify a
gate, and cannot be promoted.

## Validation and sealed tail

The first 80% of synchronized time is the selection window. Decisions stop 30
minutes before the boundary so their vertical barriers cannot cross into the
final 20%. The final 20% is not evaluated unless every selection gate passes:

- zero missing minutes;
- at least 100 selection trades and 35 in each chronological half;
- average net at least +3 bps per trade;
- profit factor at least 1.20;
- gross expectancy greater than average modeled cost;
- 0.10–4.00 trades per day;
- positive net in both chronological selection halves.

Passing these gates is hypothesis evidence only, not paper or live approval.
The final 20% may be opened once. Parameters may not be changed on this window.

The machine-readable authority is
`configs/research/btc_eth_lead_lag_v1.yaml`.
