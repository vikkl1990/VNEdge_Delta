# VNEDGE Delta India Scalper Engine v1

See [the deployed system architecture](DELTA_SCALPER_SYSTEM_ARCHITECTURE.md)
for the component map, signal sequence, live/replay assembly, and promotion
boundary.

## Status

Research-only. The engine has no broker, account client, order manager, or
submission method. A selected candidate can be converted to an `OrderIntent`
and evaluated by the existing `ScalperRiskGateway`; submission remains the
responsibility of VNEDGE's normal journaled execution path after promotion.

## Implemented flow

1. Delta public REST history seeds 1m, 5m, 15m, 1h, and 4h state.
2. Delta public WebSocket updates L2, trades, funding, ticker, and candles.
3. A candle is stored only after the next interval proves it closed.
4. The context builder computes the same deterministic features used by replay.
5. The regime engine classifies quiet, trending up/down, expanding, funding
   extreme, or unknown.
6. Momentum Burst and Imbalance Fade evaluate completed 1m/5m bars.
7. L2 imbalance and CVD are recorded as confirmation fields only. They cannot
   create, suppress, route, or promote a signal.
8. Candidates are costed with maker/taker fees, GST, optional DETO discount,
   slippage, opt-in Scalper Offer eligibility, and the applicable hold window.
9. Probability, confidence, and fee-adjusted expectancy gates rank the best
   candidate and journal the decision exactly once per scanner/market/side/bar.
10. The dashboard consumes the research snapshot. `can_trade` and
    `can_promote` remain false.

## Complete-module HLD coverage

- Public ingestion uses Delta REST backfill plus heartbeat/reconnecting WS.
  Timestamp gaps schedule an automatic REST repair. Optional venue sequences
  are tracked per channel; Delta L2 messages are full snapshots, so reconnect
  itself restores book truth.
- Private orders/fills reuse VNEDGE's existing `CcxtPrivateStream` and
  `PrivateStreamEventApplier`. The public shadow process intentionally has no
  credentials and cannot construct that path.
- The L2/trade store exposes depth, raw/z-scored imbalance, rolling CVD,
  aggression ratio, absorption score, and sequence health.
- Candle features cover EMA stack, 1h/4h trend context, ADX, ATR/percentile,
  Bollinger width, RSI/ROC, relative volume, and volume-delta proxy. Funding
  rate, velocity, and percentile plus L2 features are attached by the context
  builder. Live and replay call the same feature function.
- Signal candidates carry a complete stop, TP ladder, time stop, and optional
  trailing-rule contract. Trailing is disabled until a policy is explicitly
  configured and replayed.
- Robust research helpers support purged CPCV, DSR, PBO, chronological second
  untouched windows, and DETO/Scalper cost sensitivity. DSR/PBO remain marked
  unavailable for a single configuration rather than manufacturing confidence.
- `/delta-scalper` exposes active regimes, hit rates, flow confirmation,
  after-cost results, compliance, fee sensitivity, and robustness evidence.
- Every accepted shadow alert is registered once, measured from the next
  1-minute bar's open, and journaled again when its stop, first target, or time
  stop resolves. Expected-versus-realized net basis points are displayed, but
  this observation path cannot submit an order.

## Cost assumptions

Defaults are configuration, not immutable venue truth:

- Maker: 2 bps before GST.
- Taker: 5 bps before GST.
- GST: 18% of trading fees.
- DETO: optional 25% fee discount.
- Scalper Offer: optional and never assumed unless explicitly enabled.
- BTCUSD/ETHUSD eligible close window: 30 minutes.
- Other eligible futures: 15 minutes.
- Modeled slippage: 1.5 bps per leg by default.

Live fill accounting must replace these assumptions with Delta's reported
effective commission.

## Run the live research scanner

```bash
.venv/bin/python -m vnedge.runtime.delta_scalper_shadow
```

Only enable account-specific benefits when they are actually active:

```bash
.venv/bin/python -m vnedge.runtime.delta_scalper_shadow --scalper-opted-in --deto
```

Output:

- `research/live_research/delta_scalper_engine_latest.json`
- `logs/delta_scalper/delta_scalper_shadow.journal.jsonl`

## Run the full causal replay

```bash
.venv/bin/python -m vnedge.research.delta_scalper_backtest \
  --start 2025-01-01 \
  --symbols BTCUSD,ETHUSD \
  --scalper-opted-in
```

The replay uses next-1m-open entries, stop-first conservative resolution when
a stop and target appear in the same bar, full-path MFE/MAE, actual hold-time
fee eligibility, and no L2 because historical L2 cannot be reconstructed from
candles. It reports every day, week, month, and quarter, rolling expectancy,
market breakdown, false-signal rate, and 1x/5x/10x/25x/50x arithmetic scenarios
on $100 margin. Those leverage scenarios do not model liquidation and are not
an execution recommendation.

## Generate the loss-attribution report

```bash
.venv/bin/python -m vnedge.research.delta_scalper_attribution
```

The report decomposes only the chronological selection window by scanner,
regime, symbol, side, UTC/IST entry hour, exit reason, hold bucket, and the full
scanner × symbol × regime cross. It reports trade share, net/gross/cost bps,
hit and false-signal rates, profit factor, MFE, MAE, 1m hold bars, and
expected-versus-realized error. A frequency-versus-expectancy dataset is
included for dashboard diagnostics. The full period and frozen final 20% remain
aggregate-only so subgroup inspection cannot silently turn validation data into
training data. Live forward outcomes are never pooled with replay. Explicit
live journal rejection reasons are attributed separately; historical rejection
counts remain unavailable until replay persists every evaluated decision.

### Structured causal regime profile

Every context also carries orthogonal research labels computed from the latest
closed 5m candles: trend strength (`strong_trend`, `weak_trend`, `range`), trend
direction, rolling ATR-percentile volatility (`high`, `medium`, `low`), and a
fixed UTC session (`asia`, `europe`, `overlap`, `us`). Trend strength requires
ADX plus 20/50 EMA separation measured in ATR units and directional agreement.
Funding-extreme and L2-health flags are metadata only. The checked-in YAML owns
every threshold, and the profile is journaled on every decision for identical
live/replay attribution. These labels do not gate trades until a separately
preregistered experiment passes selection and untouched validation.

### Change-point research

The same structured profile carries a frozen, two-sided sequential CUSUM over
5m log returns and log true-range bps. Its rolling baseline is computed before
the current closed candle is observed, duplicate context builds are idempotent,
and every decision records the shift type, score, bars since shift, and the
`00-30m`, `30-60m`, `01-04h`, or `04h+` attribution window. CUSUM is metadata
only and cannot create, suppress, route, or execute a signal.

Run the separate full-history PELT diagnostic with:

```bash
.venv/bin/python -m vnedge.research.delta_scalper_change_points
```

It applies piecewise-mean PELT separately to 1m log returns and realized
volatility using every 1m observation with a disclosed 15m change-candidate
grid, then overlays merged change points on accepted-trade regime-label
transitions, and measures selection-period expectancy after a detected change.
PELT uses the complete series and is therefore explicitly future-aware: its
labels are never eligible for live scanner gates. The frozen final window stays
aggregate-only in both the PELT and CUSUM reports.

## Run the preregistered threshold sweep

```bash
.venv/bin/python -m vnedge.research.delta_scalper_threshold_sweep \
  --scalper-opted-in
```

The sweep generates the candle/scanner candidate ledger once and replays 20
single-family probability, confidence, expectancy, move-size, and fee-multiple
gates through independent next-open trade states. Configurations are ranked on
the fixed selection period. The frozen tail is evaluated once only if a variant
first clears the preregistered selection gates; otherwise it remains unopened.
Historical L2 is not fabricated and no L2 hard-gate result is claimed.

## Run the preregistered hard-regime experiment

```bash
.venv/bin/python -m vnedge.research.delta_scalper_regime_sweep \
  --scalper-opted-in
```

This experiment preserves the predictor, fee gates, candidate definitions,
entry path, and exits. It changes only scanner-specific allow-lists over the
existing causal regime labels. Filter decisions are journaled, accepted
candidates remain self-describing, and the frozen tail stays unopened unless a
selection-only variant first clears every preregistered gate.

## Promotion gates

Paper trading remains locked until untouched results show all of:

- positive after-cost results in at least two markets;
- profit factor above 1.2 after costs;
- at least two positive months;
- no market or profitable month contributing more than 70%;
- complete-candle, no-repaint causal parity;
- Scalper window compliance.

Failure of any gate leaves the engine in research mode.

## Run the chronological meta-label experiment

```bash
.venv/bin/python -m vnedge.research.delta_scalper_meta_label \
  --scalper-opted-in
```

The secondary model is a deterministic, regularized logistic classifier trained
only on earlier resolved primary-scanner outcomes. Its inputs are fields known
at the decision close: scanner, market, side, regime profile, session, CUSUM
state, predicted move/net, primary probability/confidence, planned stop/target,
ATR/BB percentiles, expected fee multiple, and UTC hour. Numeric fields use a
training-only `RobustScaler`; categoricals use a training-only one-hot encoder.
The label is realized net above +4 bps. The first 70% of total time trains, the
next 10% validates, and the final 20% remains frozen, with a 30-minute embargo
on each side of split boundaries. Historical L2 and funding are explicitly
excluded rather than reconstructed. Twenty-three preregistered probability
thresholds from 0.50 through 0.94 own independent next-open simulations from the
shared candidate ledger. The frozen tail remains unopened unless a threshold
first clears minimum sample, frequency, validation PF, average-net,
positive-market, and data-quality gates. Support evidence is written under
`research/meta_labeling`; a model pipeline and threshold are written only after
untouched success, and are never automatically loaded into the live scanner.

### Triple-barrier meta-label variant

The same runner can label a candidate by its causal exit path instead of its
fixed net return:

```bash
.venv/bin/python -m vnedge.research.delta_scalper_meta_label \
  --label-mode triple_barrier \
  --artifact-dir research/meta_labeling_triple_barrier \
  --output research/live_research/delta_scalper_meta_label_triple_barrier_latest.json \
  --scalper-opted-in
```

The binary positive class is `target_1` touched before the stop or configured
time stop. The upper target, lower stop, and vertical time barrier are rebased
to the next 1m open and recorded on every backtest and forward outcome. If stop
and target touch in the same candle, the stop wins conservatively. This variant
uses the identical chronological split, embargo, feature pipeline, threshold
grid, and frozen-window promotion safeguards as the net-bps experiment.

Each journal row also includes a nested `triple_barrier` object with the first
barrier, distances, configured vertical bars, bars-to-touch, same-bar flag, and
the effective target multiplier versus the predicted move. The mirror contract
uses scanner `target_1`: currently 0.70× predicted move for momentum and 0.65×
for imbalance fade. A 1.0×, 1.2×, or 1.5× predicted-move target is therefore a
different research exit contract and must be independently re-simulated; it
cannot be reconstructed safely from MFE after the original target already
closed the observation.

Export the existing resolved outcomes to a flat Parquet training set with:

```bash
.venv/bin/python -m vnedge.research.delta_scalper_triple_barrier_labels
```

The exporter reads `markets.*.trades` from the backtest report as well as nested
forward-journal rows. It preserves the original columns and adds `tb_label`,
`tb_first_barrier`, `tb_label_source`, `tb_tp_distance_bps`, `net_positive`, and
`net_gt_4bps`. Explicit shared-simulator labels take priority. Strict time stops
remain failures; legacy MFE recovery is available only through
`--allow-mfe-time-stop-recovery` and is marked as an approximation.

### LightGBM meta-label diagnostic

Install the research dependencies, create the Route A Parquet, then run:

```bash
.venv/bin/python -m vnedge.research.delta_scalper_lightgbm_meta
```

The experiment uses disjoint chronological windows: 60% model fitting, 10%
early stopping, 10% probability-threshold selection, and a frozen final 20%,
with a 30-minute embargo at every boundary. It uses only causal columns present
in the Parquet; historical L2, CVD, and funding are excluded rather than filled
with fabricated values. The preregistered 0.45–0.875 threshold grid is evaluated
on selection only. The final 20% remains sealed unless sample, frequency,
after-cost PF, average-net, positive-market, and source-data-quality gates all
pass. Research reports and feature importance are always written under
`research/meta_labeling_lightgbm`; a model bundle and threshold are written only
after untouched success and are never loaded into the live scanner automatically.

### CUSUM interaction attribution

Run the selection-only five-way interaction matrix with:

```bash
.venv/bin/python -m vnedge.research.delta_scalper_cusum_interactions
```

Cells are `CUSUM window × scanner × symbol × trend regime × volatility regime`.
At least 100 trades are required for an eligible cell; 80–99-trade cells are
reported separately as near-threshold diagnostics. Each cell includes after-cost
expectancy, PF, MFE/MAE, sample share, uplift versus the selection baseline, and
a 95% interval for average net bps. The frozen final 20% remains aggregate-only.
CUSUM remains journaled metadata for meta-labeling: this report cannot enable
`require_shift`, `avoid_shift`, BOCPD, PELT gates, paper trading, or execution.
The command also writes the requested ≥80-trade Parquet view to
`research/live_research/cusum_interaction_attribution.parquet`, best/worst 15
CSVs, and Imbalance Fade pivot tables for average net, PF, and trade count.
Any cell-derived binary meta feature requires a new nested discovery/validation
split; a cell discovered on this selection window is not fed back into a model
evaluated on the same window.

### SHAP attribution for the LightGBM meta-label model

Run the selection-only model explanation with:

```bash
.venv/bin/python -m vnedge.research.delta_scalper_lightgbm_shap
```

The command recreates the guarded LightGBM experiment and computes SHAP only on
the 10% probability-threshold selection window. It writes global base/encoded
importance, causal CUSUM-window attribution, five-way grouped diagnostics with
at least 40 observations, ten local high-probability explanations, a beeswarm,
global bar chart, and a waterfall under `research/meta_labeling_shap`. One-hot
contributions are added back to their causal base feature. The frozen final 20%
receives neither predictions nor SHAP values. These explanations are diagnostics
only: they cannot alter scanner thresholds, promote a model, route an order, or
invent missing historical L2/CVD inputs.

Flat CSV views are also written for scanner × volatility, scanner × CUSUM, and
scanner × trend × volatility × CUSUM. Each row carries trade count, realized
after-cost expectancy, PF, win rate, average model probability, and one column
per causal SHAP contribution. The LightGBM split-importance table is retained as
a comparison, but no model, scaler, or deployment threshold is saved while the
selection profitability gates fail.

If a future configuration clears both selection and untouched success gates,
the research trainer additionally writes a native `meta_model_lgbm.txt` Booster,
the fitted `meta_preprocessor.joblib`, and an approval-bearing `meta_config.json`.
Saved-model SHAP must load the native model with `lightgbm.Booster(model_file=...)`;
`load_approved_booster_artifacts` enforces the approval flags and complete bundle
before returning that Booster. It does not reconstruct an `LGBMClassifier` and
does not enable live integration.

The loader fails with explicit paths and remediation context for missing or
invalid configuration, missing bundle members, corrupt JSON, invalid feature
schema, path traversal, checksum failure, unreadable Booster/preprocessor files,
and Booster/preprocessor dimensional mismatch. It never fills absent model
features with zeros and never falls back to an unapproved model.
