# Kronos Forecast Gate

## Why Kronos Is Useful

[Kronos](https://github.com/shiyu-coder/Kronos) is a foundation model for
financial candle sequences. It uses an OHLCV tokenizer plus an autoregressive
Transformer to forecast future K-line paths.

For VNEDGE, the useful primitive is not "model says buy" or "model says sell".
The useful primitive is:

> Given the last closed candle window, does the forecasted future path have
> enough expected room to clear exchange fees, slippage, and adverse path risk?

That makes Kronos a **forecast gate** above existing scanners, not a standalone
trading strategy.

## Implemented Components

`vnedge.research.kronos_forecast_gate` scores already-generated forecast paths:

- auto-selects long/short unless a side is supplied
- supports maker-taker and taker-taker cost assumptions
- measures terminal move, favorable move, adverse move, reward/risk, confidence
- requires expected net edge after costs and safety buffer
- stays read-only: `can_trade=false`, `can_promote=false`

`vnedge.research.kronos_inference` is the isolated model adapter:

- validates contiguous, immutable closed-candle context
- rejects stale decisions and future/naive timestamps
- pins the upstream source, model, and tokenizer to full revisions
- generates separately seeded paths instead of accepting upstream's internally
  averaged `sample_count` output
- repairs independent-field OHLC geometry only by conservative envelope and
  discloses every repair in the artifact
- rejects non-finite/non-positive output
- hashes the canonical payload with SHA-256 and verifies it on load
- atomically publishes a research-only JSON artifact

`vnedge.research.kronos_forecast_backtest` is the chronological diagnostic:

- decision after a closed candle
- entry at the next candle open
- exit at a fixed vertical barrier close
- actual MFE, MAE, direction accuracy and terminal forecast error
- route costs deducted from every observation
- full, selection, evaluation-tail, gate-only and side-level economics
- the evaluation tail is explicitly **not** called untouched without a separate
  preregistration

`vnedge.research.kronos_permutation_matrix` is the bounded exhaustive research
runner:

- aggregates the contiguous 1m cache into complete epoch-aligned 1m, 3m, 5m,
  15m, 30m, 1h, 2h, 4h, 6h, 12h, and 1d candles
- declares the complete Cartesian grid before inference
- batches pinned Kronos inference and reuses identical sealed paths across gate
  and cost-route permutations
- reserves the final 20% of the matrix window and does not inspect it during
  selection
- requires at least four forecast paths before confidence can qualify a row
- records every permutation, failure, hash, and non-trading verdict

The separate frozen-holdback evaluator accepts exactly one previously selected
permutation. It rejects a modified selection report and cannot switch winners
after the result is known.

All four modules remain `can_trade=false` and `can_promote=false`.

## Exhaustive Matrix Result (8 August 2026)

The broad real-model run completed every declared combination:

| Item | Result |
|---|---:|
| Markets | BTCUSD, ETHUSD |
| Timeframes | 11 (1m through 1d) |
| Base AI configurations | 100 |
| Forecast observations | 1,200 |
| Scored economic permutations | 10,800 |
| Failed base runs | 0 |

That broad pass used only 12 observations per AI configuration, so none of its
rankings were allowed to qualify as economic evidence. A controlled 1h/2h/4h
confirmation then evaluated 32 base configurations with 60 chronological
observations each and scored 3,456 declared permutations. After excluding the
degenerate one-path confidence cases, 136 rows cleared the exploratory
selection screen. Many were threshold aliases of the same economic outcomes;
they are not 136 independent edges.

The balanced frozen candidate was ETHUSD 4h, 64-bar context, one-bar horizon,
four forecast paths, maker-taker costs, minimum predicted net 25 bps,
confidence 0.50, and reward/risk 1.20:

| Window | Accepted | Avg net/trade | Profit factor |
|---|---:|---:|---:|
| Selection | 42 | +36.65 bps | 2.829 |
| Reserved matrix tail | 44 | **-2.50 bps** | **0.937** |

The frozen rule therefore failed its holdback screen. Its monthly holdback
economics were strongly regime-dependent: April was positive, May through July
were negative, and June alone averaged -71.32 bps on accepted forecasts. This
is evidence of selection overfit/regime dependence, not a promotion-grade AI
edge. `can_trade` and `can_promote` remain false.

## Pinned Local Setup

The source checkout and Hugging Face artifacts are ignored local dependencies:

```bash
git clone https://github.com/shiyu-coder/Kronos.git models/kronos/upstream
git -C models/kronos/upstream checkout 67b630e67f6a18c9e9be918d9b4337c960db1e9a
python -m pip install -e '.[foundation-forecast]'
```

Pinned defaults:

| Component | Revision |
|---|---|
| Kronos source | `67b630e67f6a18c9e9be918d9b4337c960db1e9a` |
| `NeoQuasar/Kronos-mini` | `f4e68697d9d5aed55cef5c96aabc3376bcad9f81` |
| `NeoQuasar/Kronos-Tokenizer-2k` | `26966d0035065a0cae0ebad7af8ece35bc1fb51c` |

Any mismatch fails closed.

## Generate One Verified Forecast

```bash
python -m vnedge.research.kronos_inference \
  --candles /path/to/BTCUSD_1h.parquet \
  --symbol BTCUSD --timeframe 1h \
  --kronos-repo models/kronos/upstream \
  --lookback 512 --horizon 12 --samples 16 \
  --seed 42 --device cpu --local-files-only \
  --out research/live_research/kronos_btcusd_latest.json
```

CPU is the evidence default because seeded sampling is reproducible. Faster
devices may be used for exploration, but their output must not be mixed into a
CPU-pinned preregistered run.

## Run The Chronological Diagnostic

```bash
python -m vnedge.research.kronos_forecast_backtest \
  --candles /path/to/BTCUSD_1h.parquet \
  --symbol BTCUSD --timeframe 1h \
  --kronos-repo models/kronos/upstream \
  --lookback 512 --horizon 12 --samples 16 --stride 24 \
  --seed 42 --device cpu --local-files-only \
  --route maker_taker \
  --out research/live_research/kronos_btcusd_backtest_latest.json
```

## Safe VNEDGE Integration

Recommended path:

1. Generate Kronos forecast paths offline for BTC/ETH/SOL/XRP/DOGE across
   5m, 15m, 1h, and 4h.
2. Feed those paths into `kronos_forecast_gate_v1`.
3. Join the gate result to existing scanner opportunities as an ex-ante feature.
4. Compare OOS:
   - raw scanner baseline
   - scanner + Kronos veto
   - scanner + Kronos route selector
5. Promote only if it improves fee-aware OOS results and survives the normal
   untouched-window review.

## Guardrails

- No model output can bypass `PreTradeRiskGateway`.
- No model output writes runtime manifests.
- No paper lane is created from this module alone.
- Taker is allowed only if forecast net edge clears taker cost plus buffer.
- Forecasts must be judged chronologically; no random split or hindsight replay.

## Why Kronos Is Not In The Trading Runtime

Kronos model inference requires Torch/Hugging Face model weights and can be
expensive on a small VM. Loading it into the live hot path would add operational
fragility before proof of edge. VNEDGE should first prove the value as a
research gate and only later decide whether forecasts run on the VM, a sidecar,
or an offline scheduled job.
