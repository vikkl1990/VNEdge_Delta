# Luxy UT Forecast Scanner

Date: 2026-07-18  
Branch: `codex/luxy-ut-forecast-scanner`  
Commit used on VM: `6e8bd69`

## What Was Built

`luxy_ut_bot_forecast_v1` is a VNEDGE-native adaptation of the supplied
UT/forecast chart workflow. It is not a Pine copy and it does not trade live.

The scanner emits:

- adaptive UT/ATR trailing stop with chop-aware widening,
- 15m day-trading trigger profile,
- 1h and 4h EMA/ER/ADX context,
- SuperTrend-style confirmation,
- RSI divergence, displacement, sweep, CHoCH, rejection, structure breaks,
- support/resistance pressure,
- trend-duration forecast fields,
- confidence, expected edge, maker fill probability, and TP1/TP2/TP3 metadata.

The route metadata is machine-readable:

- `expectedEdge=<bps>` feeds the execution edge router.
- `fillProbability=<0..1>` feeds maker/fallback route selection.

## Important Design Decision

Continuations were tested first and overfired. The default is therefore
**flip-only**. Continuation entries remain available as an explicit research
parameter, but they are not the production research default.

That matters:

| Variant | Scope | Opportunities | Result |
|---|---:|---:|---|
| Continuations on | Delta ETH 15m, 30d | 133 | `-6.27 bps`, PF `0.81` |
| Flip-only | Delta ETH 15m, 30d | 15 | `+11.72 bps`, PF `1.43`, under-sampled |
| Continuations on | Universe 15m, 30d | 2,364 | raw `-5.46 bps`, model `-11.01 bps` |
| Flip-only | Universe 15m, 30d | 351 | raw `-11.54 bps`, model `+36.63 bps`, 6 trades |

The bot improves because the scanner now creates cleaner, feature-rich
opportunity rows and the model can find a profitable OOS pocket. It is **not
ready for paper** because the selected pocket has only 6 trades against the
20-trade minimum.

## 18-Lane Flip-Only Router Proof

VM command family:

```bash
python -m vnedge.research.execution_edge_router \
  --data-root /data \
  --exchange <exchange> \
  --symbol <symbol> \
  --timeframe 15m \
  --strategies luxy_ut_bot_forecast_v1 \
  --lookback-days 30 \
  --horizon-bars 16 \
  --min-samples 20 \
  --min-edge-bps 25 \
  --min-profit-factor 1.5
```

| Exchange | Symbol | Routed/Opp | PF | Avg Net Bps | Verdict |
|---|---:|---:|---:|---:|---|
| delta_india | BTC/USD:USD | 17/17 | 0.79 | -5.93 | UNDER_SAMPLED |
| delta_india | ETH/USD:USD | 15/15 | 1.43 | +11.72 | UNDER_SAMPLED |
| delta_india | SOL/USD:USD | 23/23 | 1.59 | +17.56 | NEGATIVE_AFTER_COST |
| delta_india | XRP/USD:USD | 23/23 | 0.96 | -1.09 | NEGATIVE_AFTER_COST |
| delta_india | DOGE/USD:USD | 19/19 | 1.34 | +8.11 | UNDER_SAMPLED |
| delta_india | BNB/USD:USD | 17/18 | 1.22 | +4.64 | UNDER_SAMPLED |
| binanceusdm | BTC/USDT:USDT | 15/16 | 1.30 | +7.32 | UNDER_SAMPLED |
| binanceusdm | ETH/USDT:USDT | 17/17 | 1.31 | +9.21 | UNDER_SAMPLED |
| binanceusdm | SOL/USDT:USDT | 22/22 | 0.78 | -10.29 | NEGATIVE_AFTER_COST |
| binanceusdm | XRP/USDT:USDT | 21/21 | 1.20 | +5.19 | NEGATIVE_AFTER_COST |
| binanceusdm | DOGE/USDT:USDT | 22/23 | 1.41 | +10.70 | NEGATIVE_AFTER_COST |
| binanceusdm | BNB/USDT:USDT | 22/22 | 0.50 | -15.63 | NEGATIVE_AFTER_COST |
| bybit | BTC/USDT:USDT | 15/16 | 1.30 | +7.80 | UNDER_SAMPLED |
| bybit | ETH/USDT:USDT | 16/16 | 1.05 | +1.65 | UNDER_SAMPLED |
| bybit | SOL/USDT:USDT | 23/23 | 0.67 | -15.01 | NEGATIVE_AFTER_COST |
| bybit | XRP/USDT:USDT | 16/17 | 1.20 | +5.74 | UNDER_SAMPLED |
| bybit | DOGE/USDT:USDT | 24/24 | 1.00 | +0.02 | NEGATIVE_AFTER_COST |
| bybit | BNB/USDT:USDT | 19/19 | 0.59 | -13.01 | UNDER_SAMPLED |

Best raw near-candidate: `delta_india SOL/USD:USD`, 23 routed events, PF
`1.59`, average net `+17.56 bps`. It clears sample/PF but fails the
`>25 bps` expected net edge floor.

Longer Delta SOL checks:

| Lookback | Routed/Opp | PF | Avg Net Bps | Verdict |
|---:|---:|---:|---:|---|
| 45d | 32/34 | 1.10 | +3.87 | NEGATIVE_AFTER_COST |
| 60d | 45/47 | 0.95 | -2.10 | NEGATIVE_AFTER_COST |
| 90d | 73/75 | 0.81 | -7.11 | NEGATIVE_AFTER_COST |

The recent SOL pocket decays when widened, so it cannot be promoted from this
evidence.

## Edge Model Proof

VM command:

```bash
python -m vnedge.research.edge_model_v1 \
  --data-root /data \
  --exchanges delta_india,binanceusdm,bybit \
  --symbols BTC/USD:USD,ETH/USD:USD,SOL/USD:USD,XRP/USD:USD,DOGE/USD:USD,BNB/USD:USD,BTC/USDT:USDT,ETH/USDT:USDT,SOL/USDT:USDT,XRP/USDT:USDT,DOGE/USDT:USDT,BNB/USDT:USDT \
  --timeframe 15m \
  --strategies luxy_ut_bot_forecast_v1 \
  --lookback-days 30 \
  --horizon-bars 16 \
  --min-train-samples 200 \
  --min-test-samples 50 \
  --min-model-trades 20 \
  --min-edge-bps 25 \
  --min-profit-factor 1.5
```

Result:

- Opportunities: `351`
- Train/test: `245 / 106`
- Raw OOS: `-11.5402 bps`, PF `0.6496`, win `34.91%`
- Model-selected OOS: `+36.6343 bps`, PF `7.1443`, win `83.33%`
- Selected trades: `6`
- Verdict: `UNDER_SAMPLED`
- Blocker: `only 6 model trades; need >= 20`

Interpretation: the feature package has signal separation, but not enough
sample count for promotion. This is the first useful next research lane:
accumulate more untouched/live-shadow examples for the model-selected pocket,
then judge it with the same 20-trade floor.

## Promotion Status

No lane is paper-ready from this PR.

Allowed next steps:

1. Keep `luxy_ut_bot_forecast_v1` in research/router sweeps.
2. Add a shadow-observation manifest only after human approval, with `can_trade=false`.
3. Pre-register a future untouched-window judgment for the model-selected
   pocket once there are at least 20 OOS examples.

Blocked actions:

- Do not promote the current 6-trade model pocket to paper.
- Do not enable continuation entries by default.
- Do not relax the `>25 bps`, PF `>1.5`, minimum 20 trade gate.
