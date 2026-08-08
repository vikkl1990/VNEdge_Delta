# Session Liquidity Sweep v1 — Preregistered Contract

Status: frozen before implementation replay. Research only; no order route.

## Hypothesis

After a completed UTC Asian range is established, a London- or New-York-window
one-minute candle that trades beyond one range extreme and closes back inside
may mean-revert far enough to overcome Delta India costs. This is a standalone
OHLCV hypothesis. It makes no L2, CVD, absorption, funding, regime, or
meta-model claim.

## Causal session resolution

The source contains candle close timestamps. Each UTC day's Asian range uses
exactly 480 closes from 00:01 through 08:00. It freezes only after the 08:00
candle closes. Therefore the causal London decision window is 08:01–08:30;
the New York window is 13:30–15:00. All endpoints are inclusive.

The Asian range must contain all 480 expected minutes, have `high > low`, and
be at least 8 bps wide relative to its midpoint. Otherwise both sessions are
disabled for that market-day.

## Decision logic

Within a decision window, a setup must satisfy every condition:

1. The candle trades above the Asian high or below the Asian low, but not both.
2. It closes strictly inside the frozen Asian high and low.
3. Sweep distance is 4–35 bps beyond the swept extreme.
4. The wick in the sweep direction is at least 55% of full candle range.
5. Volume is at least 1.40× the SMA of the preceding 20 bars, excluding the
   decision candle.
6. Current ATR(20), including the decision candle, has percentile rank at or
   below 0.85 against the preceding 100 completed ATR(20) observations.

An upside sweep produces a short setup; a downside sweep produces a long.
Setup identity is scanner, symbol, UTC session date, session name, and side.
The first emitted setup consumes that market-session even if its next-open
geometry is rejected. Only one pending or open observation may exist per
market.

## Next-open geometry and costs

Entry is the next 1m open. The fixed stop is 2 bps beyond the recorded sweep
extreme. At entry, the engine recomputes the actual stop distance. It cancels
entry when the stop is already crossed or the 1R target distance is below
3.50× modeled round-trip cost.

Both legs are taker: 5 bps pre-tax fee per leg, 18% GST, and 1.5 bps slippage
per leg, for 14.8 bps modeled round-trip cost. Thus v1 requires at least 51.8
bps structural target distance. The vertical barrier is 45 minutes; stop wins
same-bar ambiguity. No trailing stop exists.

The structural probability and confidence placeholders are uncalibrated and
have no promotion authority.

## Selection and sealed tail

The first 80% of time is selection, with a 45-minute split embargo. The final
20% remains uncomputed unless every selection gate passes:

- zero missing minutes and zero unresolved observations;
- at least 80 completed trades;
- average gross return greater than average modeled cost;
- average net return greater than zero;
- PF at least 1.15;
- at least one positive market;
- false-signal rate below 70%;
- neither market supplies more than 75% of completed trades.

If selection passes, the tail may be opened once. Untouched success requires
average net above +3 bps, PF at least 1.30, clean data, stable low frequency,
and both markets positive unless a human peer review documents a defensible
single-market exception. No result can set `can_trade` or `can_promote` true.

Failure permanently retires v1. No post-result parameter tuning is allowed on
this window. The machine-readable authority is
`configs/research/session_liquidity_sweep_v1.yaml`.
