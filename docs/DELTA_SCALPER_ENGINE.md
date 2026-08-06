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
regime, symbol, side, UTC/IST entry hour, exit reason, hold bucket, and selected
two-factor clusters. It reports net/gross/cost bps, hit and false-signal rates,
profit factor, MFE, MAE, and expected-versus-realized error. The frozen final
20% remains aggregate-only so it cannot be used for threshold selection. Live
forward outcomes are reported separately and are never pooled with replay.

## Promotion gates

Paper trading remains locked until untouched results show all of:

- positive after-cost results in at least two markets;
- profit factor above 1.2 after costs;
- at least two positive months;
- no market or profitable month contributing more than 70%;
- complete-candle, no-repaint causal parity;
- Scalper window compliance.

Failure of any gate leaves the engine in research mode.
