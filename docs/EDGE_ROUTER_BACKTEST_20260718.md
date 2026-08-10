# Edge Router Backtest - 2026-07-18

Branch: `codex/edge-router-opportunity-labeler`  
Commit tested: `bda7800`  
Runtime: VM Docker image `vnedge-research-loop` with `PYTHONPATH=/work/src`  
Policy: research-only, `can_trade=false`, `can_promote=false`

## What Was Tested

The new `execution_edge_router_v1` was run against recorded VM candle data.
Route decisions are ex-ante:

- If a scanner event has no edge forecast, it is routed as a maker baseline
  when maker fill confidence is acceptable.
- Taker and maker-then-taker fallback require an explicit ex-ante
  `expected_edge_bps` field that clears the stronger taker buffer.
- Forward labels are used only after the route choice to score outcome.

This prevents the classic fake result where hindsight skips every losing
scanner event.

## Delta ETH 5m

Command scope:

- Exchange: `delta_india`
- Symbol: `ETH/USD:USD`
- Timeframe: `5m`
- Lookback: 30 days
- Horizon: 12 bars
- Gate: average net edge >= 25 bps and PF >= 1.5

| Strategy | Opportunities | Avg Net Bps | PF | Win Rate | Verdict |
|---|---:|---:|---:|---:|---|
| `stealth_trail_bbp_v1` | 36 | -4.8611 | 0.7990 | 25.00% | `NEGATIVE_AFTER_COST` |
| `human_trade_fingerprint_v1` | 36 | -4.8611 | 0.7990 | 25.00% | `NEGATIVE_AFTER_COST` |
| `sats_5m_scalper_v1` | 264 | -10.4194 | 0.5811 | 35.23% | `NEGATIVE_AFTER_COST` |
| `alpha_stack_confluence_v1` | 259 | -11.1022 | 0.5480 | 34.75% | `NEGATIVE_AFTER_COST` |
| `quant_signal_pack_v1` | 138 | -15.0979 | 0.3872 | 30.43% | `NEGATIVE_AFTER_COST` |
| `smc_playbook_scalper_v1` | 0 | n/a | n/a | 0.00% | `NO_OPPORTUNITIES` |

Read: the raw 5m scanner entries are not a profitable trade policy. The least
bad family is still negative after maker-first route costs.

## 15m Cross-Venue Sweep

Command scope:

- Exchanges: `binanceusdm`, `bybit`, `delta_india`
- Symbols: BTC, ETH, SOL, BNB, XRP, DOGE
- Timeframe: `15m`
- Lookback: 30 days
- Horizon: 8 bars
- Gate: average net edge >= 25 bps and PF >= 1.5

Aggregate:

- Reports: 108
- Opportunities: 10,534
- Routed maker baselines: 10,533
- Verdicts: 90 `NEGATIVE_AFTER_COST`, 18 `NO_OPPORTUNITIES`
- Passing lanes: 0

Best observed lanes, still rejected:

| Exchange | Symbol | Strategy | Opportunities | Avg Net Bps | PF | Win Rate | Verdict |
|---|---|---|---:|---:|---:|---:|---|
| `delta_india` | `ETH/USD:USD` | `alpha_stack_confluence_v1` | 155 | 9.0483 | 1.2668 | 46.45% | `NEGATIVE_AFTER_COST` |
| `bybit` | `SOL/USDT:USDT` | `stealth_trail_bbp_v1` | 72 | 8.4655 | 1.2244 | 41.67% | `NEGATIVE_AFTER_COST` |
| `bybit` | `SOL/USDT:USDT` | `human_trade_fingerprint_v1` | 72 | 8.4655 | 1.2244 | 41.67% | `NEGATIVE_AFTER_COST` |
| `binanceusdm` | `ETH/USDT:USDT` | `alpha_stack_confluence_v1` | 160 | 6.7886 | 1.1947 | 46.88% | `NEGATIVE_AFTER_COST` |
| `delta_india` | `SOL/USD:USD` | `quant_signal_pack_v1` | 118 | 6.6769 | 1.1677 | 48.31% | `NEGATIVE_AFTER_COST` |

Worst observed lanes:

| Exchange | Symbol | Strategy | Opportunities | Avg Net Bps | PF | Win Rate | Verdict |
|---|---|---|---:|---:|---:|---:|---|
| `bybit` | `DOGE/USDT:USDT` | `sats_5m_scalper_v1` | 179 | -21.4030 | 0.5122 | 39.11% | `NEGATIVE_AFTER_COST` |
| `delta_india` | `DOGE/USD:USD` | `alpha_stack_confluence_v1` | 161 | -21.2845 | 0.4890 | 34.16% | `NEGATIVE_AFTER_COST` |
| `binanceusdm` | `DOGE/USDT:USDT` | `alpha_stack_confluence_v1` | 160 | -20.1587 | 0.5360 | 35.62% | `NEGATIVE_AFTER_COST` |
| `bybit` | `DOGE/USDT:USDT` | `alpha_stack_confluence_v1` | 160 | -19.5663 | 0.5593 | 36.88% | `NEGATIVE_AFTER_COST` |
| `binanceusdm` | `DOGE/USDT:USDT` | `quant_signal_pack_v1` | 106 | -18.5555 | 0.6006 | 34.91% | `NEGATIVE_AFTER_COST` |

## Conclusion

The scanners are not useless as feature generators, but they are failures as
standalone trading policies. The evidence says:

- Raw scanner events do not clear the 25 bps / PF 1.5 route gate.
- The closest lanes have small positive average net edge, but not enough to
  justify paper promotion.
- `stealth_trail_bbp_v1` and `human_trade_fingerprint_v1` are useful features,
  not executable entries yet.
- The next required build is an ex-ante edge model that learns from every
  opportunity row: scanner features, route costs, forward MFE/MAE, and net
  result.

Next build:

1. Persist compact opportunity feature rows from `execution_edge_router_v1`.
2. Train `edge_model_v1` to predict `expected_net_edge_bps`,
   `tp_before_sl_probability`, and `maker_fill_probability`.
3. Re-run the router using model predictions instead of raw scanner events.
4. Only promote lanes whose model-routed results clear the same 25 bps / PF 1.5
   gate on fresh data.
