# VNEDGE Algo ML Pro Scanner

`vnedge_algo_ml_pro_v1` is the VNEDGE-native scanner for the supplied
open/user-provided Pine source: `VNEDGE ALGO SuperTrend ML Pro + BBP`
(`v6.0.1`).

## What Was Ported

- Adaptive SuperTrend-style flip engine.
- Bull Bear Power: `high - EMA13`, `low - EMA13`, and normalized pressure.
- Auto HTF alignment: 5m uses 30m by default; 15m uses 1h.
- RSI momentum, ADX strength, volume impulse, volume-zone proximity, and
  divergence features.
- Pine-style ML confidence score from the 13 feature weights.
- Band/ATR/fixed stop geometry, TP1/TP2/TP3 ladder, and trailing-plan metadata.

## What Was Not Ported

- Chart-only dashboard tables, labels, background fills, line drawing, and
  spectral forecast visuals.
- Pine self-learning gate mutation in live time. VNEDGE keeps learning in the
  research layer so the live path remains auditable and reproducible.
- Any live permission. The scanner emits research/paper intents only.

## Paper Sizing Lens

The execution-edge router now reports paper USD PnL using:

- `paper_margin_usd = 100`
- `paper_leverage = 25`
- `paper_notional_usd = 2500`

Delta India live compatibility note:

- VNEDGE order intents carry base quantity, but Delta native orders require
  integer contract counts. ETHUSD is not `0.2` native size; with the current
  Delta product contract value (`0.01 ETH`), `0.2 ETH` is `20` contracts.
- Pine-parity replay therefore has two explicit lenses:
  `fixed_notional` preserves older `$100 x 25 = $2500` proof comparisons, while
  `delta_contract_risk` mirrors the Pine position-size block:
  risk USD = account equity x risk %, quantity = risk / stop distance,
  rounded down to Delta contracts, then margin = actual notional / effective
  leverage.
- Use `--delta-live-product-spec --sizing-mode delta_contract_risk` for current
  Delta India product metadata. High leverage follows the Pine/VNEDGE clamp:
  >10x requires `--acknowledge-high-leverage`; otherwise 25x displays and
  replays as 10x clamped.

This is a reporting lens only. It does not change live leverage caps, position
sizing, risk-per-trade, or the `PreTradeRiskGateway`.

Example one-lane replay:

```bash
python -m vnedge.research.execution_edge_router \
  --data-root data \
  --exchange delta_india \
  --symbol ETHUSD \
  --timeframe 5m \
  --strategies vnedge_algo_ml_pro_v1 \
  --lookback-days 30 \
  --horizon-bars 15 \
  --min-samples 20 \
  --min-edge-bps 25 \
  --min-profit-factor 1.5 \
  --paper-margin-usd 100 \
  --paper-leverage 25
```

Pine-parity replay (same entry/exit lifecycle as the supplied TradingView
indicator):

```bash
python -m vnedge.research.vnedge_algo_ml_pro_pine_replay \
  --data-root data \
  --exchange delta_india \
  --symbol ETHUSD \
  --timeframe 5m \
  --lookback-days 30 \
  --paper-margin-usd 100 \
  --paper-leverage 25
```

Smart-capture replay (VNEDGE bot overlay; not exact TradingView parity):

```bash
python -m vnedge.research.vnedge_algo_ml_pro_pine_replay \
  --data-root data \
  --exchange delta_india \
  --symbol ETHUSD \
  --timeframe 5m \
  --lookback-days 30 \
  --paper-margin-usd 100 \
  --paper-leverage 25 \
  --capture-mode smart_ladder
```

The Pine-parity replay is deliberately separate from the generic fee-wall
router. It uses the indicator's chart lifecycle exactly:

- enter on the confirmed signal bar's close;
- first TP/SL check starts on the next confirmed bar (`+1` bar after entry);
- there is no fixed exit wait: trades remain open until SL, TP3, reverse, or
  the final open-position mark;
- stop out only when the bar closes beyond the trailing stop;
- mark TP1/TP2/TP3 on wick touch, with only TP3 closing the whole trade;
- update the trailing stop after TP/SL checks;
- reverse at the current signal close when the opposite signal fires.

The Pine input `Evaluation Horizon (bars) = 15` belongs to the script's
self-learning ML gate. It does not close trades after 15 bars. The replay records
that distinction in `summary.bar_timing` so the TradingView comparison is
auditable.

The report shows both `visual_*` results, which match the indicator's no-fee
chart economics, and `fee_aware_*` results, which subtract the venue taker
round-trip cost. Only fee-aware evidence can ever feed VNEDGE promotion review.

`smart_ladder` is a research-only bot overlay for the question "should VNEDGE
bank or protect the movement instead of waiting passively for TP3?" By default
it keeps the full runner, moves the stop to breakeven after TP1, and can lock
TP1 after TP2. Optional `--tp1-capture-fraction` and
`--tp2-capture-fraction` allow partial banking, but the first VM sweep found
that early partials improved win rate while reducing expectancy. This is not
exact Pine parity, but it is the right execution experiment for fee-wall
recovery.

Batch forensics:

```bash
python -m vnedge.research.fee_wall_forensics \
  --data-root data \
  --exchanges delta_india \
  --symbols ETHUSD \
  --timeframes 5m \
  --strategies vnedge_algo_ml_pro_v1 \
  --lookback-days 30 \
  --min-edge-bps 25 \
  --min-profit-factor 1.5 \
  --paper-margin-usd 100 \
  --paper-leverage 25 \
  --include-opportunities
```

## Promotion Rule

This scanner remains research-only until an untouched-window judgment clears
the usual VNEDGE gates. For the requested scalper profile the promotion target
is:

- expected net edge greater than `25 bps`;
- profit factor greater than `1.5`;
- at least `20` historical routed trades;
- taker only when forecast edge covers fees, slippage, and safety buffer.

## First VM Backtest

Run on the VM against normalized Delta India `ETHUSD` candles with the paper
reporting lens `100 USD margin x 25x = 2,500 USD notional`.

Command scope:

- exchange: `delta_india`
- symbol: `ETHUSD`
- timeframes: `1m, 5m, 15m, 1h, 4h`
- lookback: `30d`
- strategy: `vnedge_algo_ml_pro_v1`
- gate: `25 bps`, `PF > 1.5`, `min 20 routed trades`

Results:

| Timeframe | Routed | Avg Net | PF | Fee-Wall Break | Verdict |
| --- | ---: | ---: | ---: | ---: | --- |
| 1h | 13 | +22.98 bps | 1.60 | 100.00% | sparse positive, below 20 trades and below 25 bps |
| 15m | 89 | +6.51 bps | 1.25 | 89.89% | positive but below edge/PF gate |
| 5m | 88 | -8.12 bps | 0.68 | 76.14% | negative after cost |
| 1m | 229 | -16.10 bps | 0.45 | 72.49% | negative after cost |
| 4h | 0 | -- | -- | 0.00% | no opportunities |

Interpretation: the scanner fires, but the raw 5m/1m form gives back too much
after fees. The useful evidence is not "promote as-is"; it is that many entries
show positive MFE after costs, so the next research uplift should test faster
target capture, BE-after-TP1, and trail tightening before trying to promote the
lane.

## Pine-Parity VM Replay

After adding the dedicated Pine-parity lifecycle replay, the same Delta India
`ETHUSD` lane was rerun with exact chart-style entry/exit mechanics:

| Timeframe | Closed Trades | Win % | PF(R) | Visual Avg | Fee-Aware Avg | Fee-Aware USD | Verdict |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| 1m | 1,626 | 33.21% | 0.83 | -2.35 bps | -14.85 bps | -$6,036.68 | fail |
| 5m | 121 | 34.71% | 1.07 | +4.12 bps | -8.38 bps | -$253.56 | fail |
| 15m | 88 | 32.95% | 1.31 | +1.22 bps | -11.28 bps | -$248.17 | fail |
| 1h | 12 | 33.33% | 1.30 | -8.94 bps | -21.44 bps | -$64.33 | fail |
| 4h | 0 | -- | -- | -- | -- | $0.00 | no trades |

Delta India 5m pair sweep:

| Pair | Closed Trades | PF(R) | Visual Avg | Fee-Aware Avg | Fee-Aware USD |
| --- | ---: | ---: | ---: | ---: | ---: |
| BTCUSD | 308 | 0.88 | -1.25 bps | -13.75 bps | -$1,058.95 |
| ETHUSD | 121 | 1.07 | +4.12 bps | -8.38 bps | -$253.56 |
| SOLUSD | 303 | 1.00 | +3.40 bps | -9.10 bps | -$689.21 |
| XRPUSD | 327 | 0.79 | -6.99 bps | -19.49 bps | -$1,593.29 |
| BNBUSD | 421 | 0.75 | -1.95 bps | -14.45 bps | -$1,521.17 |
| DOGEUSD | 365 | 0.73 | -3.74 bps | -16.24 bps | -$1,482.04 |

Smart-capture comparison on Delta India `ETHUSD` 5m:

| Mode | Closed Trades | Win % | PF(R) | Visual Avg | Fee-Aware Avg | Fee-Aware USD | Avg Hold |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `pine_tp3` | 121 | 34.71% | 1.07 | +4.12 bps | -8.38 bps | -$253.56 | 16.54 bars |
| `smart_ladder` default | 121 | 37.19% | 1.19 | +5.44 bps | -7.06 bps | -$213.60 | 14.77 bars |
| `smart_ladder` 35%/35% partial | 121 | 49.59% | 1.08 | +2.55 bps | -9.95 bps | -$301.06 | 14.77 bars |

Conclusion: the TradingView parity gap is fixed. The chart lifecycle explains
why ETH/SOL can look mildly positive before fees, but no tested lane clears the
Delta taker fee wall. The next build should not keep retesting this exact entry;
the default smart runner improves the result but remains negative after costs.
The next build should test a VNEDGE-owned execution uplift around this signal:
maker-first entry, stricter participation filter, faster invalidation, and taker
fallback only when forecasted move exceeds fees plus buffer.

## Repeatable Contract-Risk Matrix

The one-off VM loop is now a first-class research command:

```bash
python -m vnedge.research.vnedge_algo_ml_pro_contract_matrix \
  --data-root data \
  --exchange delta_india \
  --symbols BTCUSD ETHUSD SOLUSD XRPUSD BNBUSD DOGEUSD \
  --timeframes 1m 5m 15m 1h 4h \
  --capture-modes pine_tp3 smart_ladder \
  --lookback-days 30 \
  --sizing-mode delta_contract_risk \
  --delta-live-product-spec \
  --account-equity-usd 500 \
  --risk-per-trade-pct 1 \
  --paper-margin-usd 100 \
  --paper-leverage 25 \
  --acknowledge-high-leverage \
  --fee-cost-bps 12.5 \
  --out research/live_research/vnedge_algo_ml_pro_contract_matrix_latest.json
```

The matching uplift agent is:

```bash
python -m vnedge.research.scanner_backtest_uplift \
  --input research/live_research/vnedge_algo_ml_pro_contract_matrix_latest.json \
  --source-name vnedge_algo_ml_pro_contract_matrix \
  --input research/live_research/scanner_tournament_latest.json \
  --source-name scanner_tournament \
  --out research/live_research/scanner_backtest_uplift_latest.json
```

Both commands are wired into Docker Compose as scheduled research-only services:

- `vnedge-algo-ml-pro-contract-matrix`
- `scanner-backtest-uplift`

The latest 30-day Delta India matrix with live contract specs produced:

- `60` rows: 6 pairs x 5 timeframes x 2 capture modes.
- `0` promotable proof candidates.
- `1` positive-after-cost sparse row: `BTCUSD 1h smart_ladder`
  (`+3.97 bps`, 11 trades), which is below both sample and 25 bps gates.
- `9` fee-wall near misses.
- `15` visual-only positives, meaning the chart can look good before costs but
  fails after fees/slippage.

Top uplift targets:

| Rank | Pair | TF | Mode | Fee-Aware Avg | PF(R) | Needed Uplift |
| --- | --- | --- | --- | ---: | ---: | ---: |
| 1 | DOGEUSD | 15m | smart_ladder | -1.44 bps | 1.20 | +26.44 bps |
| 2 | BTCUSD | 15m | smart_ladder | -3.18 bps | 1.35 | +28.18 bps |
| 3 | BTCUSD | 15m | pine_tp3 | -3.86 bps | 1.40 | +28.86 bps |
| 4 | DOGEUSD | 15m | pine_tp3 | -3.29 bps | 1.15 | +28.29 bps |
| 5 | XRPUSD | 15m | pine_tp3 | -6.62 bps | 1.48 | +31.62 bps |

Architecture read: the candidate family is not dead as a visual detector, but it
is not an executable bot edge yet. The correct next experiment is
`maker_first_context_filtered_replay` on 15m DOGE/BTC/XRP and ETH 5m/15m:
require HTF bias, BBP/ADX agreement, volume impulse, room-to-liquidity, and
maker-first route evidence before any taker fallback.
