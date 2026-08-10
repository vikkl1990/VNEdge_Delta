# BTC→ETH Transfer Entropy v1 — Frozen Result

## Verdict

Rejected. Transfer entropy found stable bidirectional non-linear information,
with BTC→ETH generally stronger, but no BTC→ETH cell reached the frozen minimum
normalized effect size. No lead-lag v2 or scanner backtest is authorized.

The complete study was frozen before computation in local commit `a0d6232`.
Configuration SHA-256:
`e047dd708b7ca99e079c75c0d5169d906e958f143ed205abf5f777e00139bc14`.
No estimator, bin, history, surrogate, window, threshold, or gate changed after
the result was observed.

## Data and protocol

| Item | Result |
|---|---:|
| Selection-only window | 2025-01-01 through 2026-04-01 23:53 UTC |
| 1m synchronized return observations | 656,517 |
| 5m complete-bucket return observations | 131,300 |
| Gap-separated segments | 3 per timeframe |
| Rolling windows | 13 × 90 days, stepped 30 days |
| Encoding | Within-window return tertiles |
| Histories | 1, 2, and 3 bars |
| Surrogates | 99 segment-preserving circular shifts per cell |
| Tests per rolling window | 12, BH-corrected |

## Primary rolling evidence

Normalized effective TE is the surrogate-bias-corrected information divided by
the uncertainty remaining in the target after its own history. The frozen gate
required at least 0.005, or 0.5%.

| TF | Direction | History | Significant windows | Positive effect | Median effective TE | Median normalized TE | Direction ratio |
|---|---|---:|---:|---:|---:|---:|---:|
| 1m | BTC→ETH | 1 | 100.00% | 100.00% | 0.004245 bits | 0.002688 | 1.215 |
| 1m | BTC→ETH | 2 | 100.00% | 100.00% | 0.006327 bits | 0.004022 | 1.287 |
| 1m | BTC→ETH | 3 | 100.00% | 100.00% | 0.007640 bits | 0.004869 | 1.278 |
| 1m | ETH→BTC | 1 | 100.00% | 100.00% | 0.003494 bits | 0.002224 | 0.823 |
| 1m | ETH→BTC | 2 | 100.00% | 100.00% | 0.004915 bits | 0.003149 | 0.777 |
| 1m | ETH→BTC | 3 | 100.00% | 100.00% | 0.005977 bits | 0.003845 | 0.782 |
| 5m | BTC→ETH | 1 | 100.00% | 100.00% | 0.003466 bits | 0.002191 | 2.042 |
| 5m | BTC→ETH | 2 | 100.00% | 100.00% | 0.005031 bits | 0.003191 | 1.734 |
| 5m | BTC→ETH | 3 | 92.31% | 100.00% | 0.007100 bits | 0.004586 | 1.705 |
| 5m | ETH→BTC | 1 | 100.00% | 100.00% | 0.001698 bits | 0.001077 | 0.490 |
| 5m | ETH→BTC | 2 | 100.00% | 100.00% | 0.002901 bits | 0.001857 | 0.577 |
| 5m | ETH→BTC | 3 | 76.92% | 100.00% | 0.004163 bits | 0.002690 | 0.586 |

## Gate interpretation

BTC→ETH passed window count, corrected significance, positive-effect stability,
and absolute effective-TE size in every cell. Histories 2–3 also passed the
1.25× directionality requirement. Every cell failed the same decisive gate:

```text
required median normalized effective TE: 0.005000
best observed rolling median:             0.004869  (1m, history 3)
best 5m rolling median:                   0.004586  (history 3)
```

The threshold will not be lowered after observing that near miss. Full-period
descriptive values also remained below 0.005 and cannot override the rolling
gate.

## Conclusion

There is weak, stable, non-linear directed dependence between BTC and ETH, and
BTC is the stronger source at 5m. The effect explains less than 0.5% of the
target's remaining conditional uncertainty and has not been translated into
returns, trade timing, or after-cost expectancy. Bidirectional significance may
also reflect common drivers rather than a tradeable causal path.

Do not condition this result on volatility or search different bins after the
fact. Such work would require another preregistration, and the locked research
sequence should instead proceed to `range_compression_breakout_v1`.

The old lead-lag final 20% remained untouched: access, predictions, and trades
are all false.
