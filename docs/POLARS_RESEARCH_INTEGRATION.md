# Polars Research Integration

## Status

Polars is integrated only into VNEDGE's offline research path. The live candle
store, shadow process, signal state machines, order safety controls, and frozen
backtest results are unchanged.

## Implemented capabilities

- Strict UTC candle schema with symbol and timeframe identity.
- Lossless conversion between `Candle` objects and Polars frames.
- Lazy native Delta Parquet scanning.
- Deterministic OHLCV validation, source-aware deduplication, and chronological sorting.
- Group-safe log returns, true range, and causal rolling ATR.
- Vectorized gap detection with missing-bar counts.
- NumPy-vectorized raw swing masks with Polars grouping and output.
- V2-compatible minimum swing filtering against the latest accepted opposite swing.
- Explicit `confirmed_at` timestamps for every right-confirmed swing.
- Reproducible Polars-versus-Pandas benchmark with parity checks.

The swing output contains both the pivot timestamp (`ts`) and its causal
availability timestamp (`confirmed_at`). Research consumers must never expose a
swing before `confirmed_at`.

## Dependency boundary

Polars remains in the optional `quant-research` dependency group. Importing the
rest of VNEDGE does not require Polars. Calling a Polars adapter without the
extra installed returns a direct installation instruction.

```bash
pip install -e '.[quant-research]'
```

## Real-cache benchmark

Benchmark source: the current local BTCUSD 1m cache, including deterministic
deduplication across 30 shards.

| Metric | Polars | Pandas |
|---|---:|---:|
| Output rows | 838,159 | 838,159 |
| Close-total parity | exact | exact |
| Best of 3 pipeline time | 0.135 s | 0.468 s |
| Measured frame memory | 132.8 MB | 152.8 MB |

Observed improvement:

- 3.46x best-time speedup;
- approximately 13.1% lower measured frame memory;
- zero timestamp gaps and zero missing bars in the deduplicated result.

These are local measurements, not universal performance guarantees. The
benchmark warms filesystem caches before timing both engines and records all
three runs in the JSON artifact.

## Integration rules

Use Polars for:

- large historical cache loading and cleaning;
- gap audits;
- batch feature and swing precomputation;
- future attribution and feature-matrix assembly.

Do not use Polars in:

- the live WebSocket callback path;
- mutable multi-timeframe state;
- order execution or risk enforcement;
- a frozen scanner without a new preregistered experiment.

## Verification

- Polars adapter and benchmark tests: 7 passed.
- Full repository suite: 1,999 passed with one unrelated third-party warning.
- No backtest or sealed untouched dataset was opened.
- No trading route or promotion flag was changed.
