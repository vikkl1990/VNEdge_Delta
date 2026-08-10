# VNEDGE AlphaStack

AlphaStack is the candle-structure package for the slow research loop. It is
inspired by the kind of visual confluence traders use manually, but it is not
a TradingView clone and it is not a live signal feed.

## What It Builds

`alpha_stack_confluence_v1` converts chart concepts into causal, testable
features:

- structure breaks and change-of-character proxies
- liquidity sweeps above/below prior structure
- equal high/low liquidity pools
- fair-value gap and displacement proxies
- order-block retest proxies
- EMA/VWAP trend alignment
- momentum and volume confirmation
- volatility regime filter
- structure-aware stop and R-multiple target

The strategy returns a normal `SignalIntent`, so every candidate still flows
through the existing backtester, walk-forward gates, shadow manifest, paper
runner, risk gateway, journal, and order manager. No AlphaStack path bypasses
the VNEDGE safety spine.

## Research Contract

AlphaStack is research-first:

- no live permission
- no auto-promotion
- closed-candle features only
- no repainting/future leakage
- offensive promotion gates in continuous research
- human approval and untouched-data judgment required before paper/shadow
  discussion

The continuous research loop runs it with a bounded grid:

```text
structure_window = 24, 48
min_score        = 5.0, 6.0
take_profit_r    = 1.5, 2.0
```

If it becomes the best profitable lane for an exchange/symbol, the
research-to-shadow bridge can emit a shadow-only manifest using locked default
params. That creates a live-data observation lane only; it does not approve
paper or live capital.

## Why This Is Different From Manual Indicators

Manual indicator users often add unstated discretion: they skip bad sessions,
avoid thin books, scale out, override noisy signals, and react to live order
flow. AlphaStack must make that discretion explicit enough to test. A signal
is only considered when structure, liquidity, momentum, volume, and volatility
agree strongly enough. The tick/L2 alpha factory remains the layer that later
tests whether those candle contexts have real microstructure edge after fees.
