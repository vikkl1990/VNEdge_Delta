# BTC→ETH Transfer Entropy v1 — Preregistered Contract

## Purpose

This study tests whether non-linear directed information from BTC returns to
future ETH returns is stable enough to justify writing a distinct lead-lag v2
hypothesis. It does not alter the rejected lead-lag v1 scanner and cannot
authorize a scanner replay or trading.

## Frozen information boundary

- Exact-timestamp synchronized BTCUSD/ETHUSD completed 1-minute candles.
- Selection-only period: 1 January 2025 through 1 April 2026 at 23:53 UTC.
- The old lead-lag final 20%, beginning 2 April 2026 at 00:23 UTC, is forbidden.
- Missing minutes break histories and surrogate rotations.
- Five-minute returns require five complete synchronized one-minute closes.

## Representation and estimator

Each rolling window independently maps log returns into three equiprobable
states using its 1/3 and 2/3 quantiles. Tests use equal source and target history
lengths of 1, 2, and 3 bars and a one-bar forecast horizon.

Transfer entropy is calculated in bits with the discrete plug-in estimator:

```text
TE(X→Y) = H(Yfuture,Ypast) + H(Ypast,Xpast)
          - H(Ypast) - H(Yfuture,Ypast,Xpast)
```

The reported effect is effective TE: observed TE minus mean surrogate TE.
Normalized effective TE divides that result by target conditional entropy.

## Rolling and surrogate protocol

- Timeframes: 1m and 5m.
- Directions: BTC→ETH and ETH→BTC.
- Rolling windows: 90 days, stepped by 30 days.
- Minimum 10,000 valid observations per cell.
- Surrogates: 99 deterministic, segment-preserving circular shifts of the
  source series, with at least a 20-bar shift.
- Empirical p-value: `(1 + null TE >= observed TE) / 100`.
- Benjamini–Hochberg correction across all 12 timeframe/direction/history tests
  in the same rolling window.
- Full-period estimates are descriptive only.

Circular shifts preserve each source segment's marginal distribution and
autocorrelation while breaking its alignment with the target. They do not
prove structural causation or control for omitted common drivers.

## Frozen advancement gate

A BTC→ETH timeframe/history cell may justify writing a separate v2 contract
only if every condition passes:

1. At least eight valid rolling windows.
2. Corrected p ≤ 0.05 in at least 60% of windows.
3. Positive effective TE in at least 70% of windows.
4. Median effective TE ≥ 0.0005 bits.
5. Median normalized effective TE ≥ 0.005.
6. Median effective TE at least 1.25× the corresponding ETH→BTC cell when the
   reverse effect is positive.

A pass only authorizes a new preregistration. It does not authorize threshold
selection, regime slicing, scanner construction, backtesting, paper mode, live
orders, or opening the old sealed tail.
