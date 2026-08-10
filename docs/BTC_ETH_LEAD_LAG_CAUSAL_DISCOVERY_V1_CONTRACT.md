# BTC–ETH Lead-Lag Causal Discovery v1 — Preregistered Contract

## Purpose

This study asks whether lagged BTC returns add stable predictive information
for ETH returns beyond ETH's own lags, and whether any effect is stronger than
the reverse ETH-to-BTC direction. It is a diagnostic study, not a scanner and
not evidence of structural causation.

It is separate from the rejected `btc_eth_lead_lag_v1` trading hypothesis. No
v1 threshold, entry, stop, or target is changed.

## Information boundary

- Input: exact-timestamp synchronized BTCUSD and ETHUSD completed 1-minute
  candles.
- Study window: 1 January 2025 through 1 April 2026 at 23:53 UTC—the already
  opened v1 selection period only.
- Forbidden window: the old v1 final 20%, beginning 2 April 2026 at 00:23 UTC.
- Missing minutes break lag chains. No return or lag may cross a gap.
- Five-minute observations are built only from five complete synchronized
  one-minute closes.

## Frozen tests

For 1-minute and 5-minute log returns, test both BTC→ETH and ETH→BTC at maximum
lags 1, 2, 3, and 6. Each nested regression compares:

```text
restricted:   target own lags
unrestricted: target own lags + source lags
```

The primary evidence is a sequence of 90-day rolling windows stepped by 30
days. Each window uses its first 70% for the nested OLS F-test and fitting, then
its final 30% for untouched-in-window prediction. P-values are corrected with
Benjamini–Hochberg across every timeframe, direction, and lag tested in that
same rolling window.

The full-period result is descriptive only. It cannot pass the study.

## Advancement gate

A specific BTC→ETH timeframe/lag may justify writing a distinct v2 contract
only when all of the following are true:

1. At least eight valid rolling windows.
2. Corrected p-value ≤ 0.01 in at least 60% of them.
3. Unrestricted out-of-sample MSE beats the restricted model in at least 60%.
4. Median out-of-sample MSE improvement is at least 0.01%.
5. Median sign-accuracy uplift is non-negative.
6. Median MSE improvement is at least 1.25 times the corresponding reverse
   ETH→BTC result when that reverse result is positive.

Passing does not authorize a scanner backtest, paper trade, model artifact, or
opening the old v1 tail. It only authorizes a new, separately versioned and
preregistered scanner hypothesis.

## Interpretation

Granger-style evidence means predictive precedence under the tested linear
model. Common drivers, omitted variables, venue effects, microstructure noise,
and costs can still make the relationship non-causal or economically useless.
The study makes no trading-profit claim.
