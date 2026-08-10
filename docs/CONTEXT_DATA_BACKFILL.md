# Context Data Backfill

`vnedge.data.context_backfill` builds the reusable candle context lanes for the
research/scalper stack:

- `4h` and `1h` for market regime and higher-timeframe bias
- `15m` for setup structure and volatility/funding context
- `1m` for trigger/replay approximation when L2 data is not enough by itself

The job is chunked and checkpointed. It writes candles through the existing
quality gate and Parquet upsert path, then records each completed chunk in
`data/reports/context_backfill/manifest.json`. Reruns skip chunks already in
the manifest and also mark existing Parquet coverage as complete, so deploying
new code does not rebuild the same historical data every time.

## Core Universe

Build the configured research universe across Binance, Bybit, and Delta India:

```bash
python -m vnedge.data.context_backfill \
  --data-root data \
  --timeframes 4h,1h,15m,1m
```

Default lookbacks:

| Timeframe | Lookback | Chunk Size |
| --- | ---: | ---: |
| `4h` | 365d | 90d |
| `1h` | 365d | 30d |
| `15m` | 180d | 14d |
| `1m` | 60d | 3d |

To rebuild only the missing higher-timeframe context:

```bash
python -m vnedge.data.context_backfill \
  --data-root data \
  --timeframes 4h,15m \
  --timeframe-days 4h=365,15m=180 \
  --chunk-days 4h=90,15m=14
```

## Active-Market Discovery

For research-only all-pair sweeps, discover active derivative markets from each
venue instead of using the fixed symbol list:

```bash
python -m vnedge.data.context_backfill \
  --data-root data \
  --discover-active \
  --exchanges binanceusdm,bybit,delta_india \
  --quote-assets USDT,USDC,USD \
  --max-symbols-per-exchange 150 \
  --timeframes 4h,1h,15m,1m
```

Keep `--max-symbols-per-exchange` bounded until disk and cycle time are
confirmed. This is research data only; it does not expand live execution scope.

## Restart Safety

The manifest is updated after every successful chunk. If the process stops,
rerun the same command and it resumes by skipping completed chunks.

Use `--dry-run` to inspect planned work:

```bash
python -m vnedge.data.context_backfill --dry-run --json
```

If a venue has legitimate historical gaps and the operator accepts that for a
specific research pass, add `--allow-gaps`. Without it, gapped candle chunks are
rejected and documented in `data/reports/data_quality/`.
