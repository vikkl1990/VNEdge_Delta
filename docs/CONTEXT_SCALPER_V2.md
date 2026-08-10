# Context Scalper V2

`context_scalper_v2` is the first VNEDGE-owned context scanner built from the
latest scalper evidence. It is not a TradingView copy. It combines the strongest
source-backed families already in the bot:

- `vnedge_algo_ml_pro_v1` for SuperTrend/BBP/ML-style flip entries.
- `stealth_trail_bbp_v1` for BBP pressure, displacement, structure and trail.

## Trading Contract

- Trigger timeframe: `5m`.
- Confirmation: completed `15m`.
- Bias: completed `1h`.
- Route: maker-first.
- Taker fallback: allowed only when expected net edge clears the configured
  fee-wall threshold plus buffer.
- Paper lens: `$100` margin and `25x` leverage are carried in the reason string
  for research/paper reporting. Sizing still goes through VNEDGE risk controls.

## Runtime Lanes

Delta India observation lanes are enabled by default:

- `ETH/USD:USD` uses engine `algo_ml`.
- `XRP/USD:USD` uses engine `stealth`.

They run in `SHADOW` unless `MULTI_LANE_PAPER_OBSERVE_ALL=1` mirrors shadow
lanes into isolated simulated paper observation ledgers. This is not a governed
paper promotion and never enables live orders.

## Backtest Command

Run the next VM proof with:

```bash
python -m vnedge.research.scanner_tournament \
  --data-root /app/data \
  --exchanges binanceusdm,bybit,delta_india \
  --symbols "BTC/USDT:USDT,ETH/USDT:USDT,SOL/USDT:USDT,XRP/USDT:USDT" \
  --timeframes 5m,15m,1h,4h \
  --strategies context_scalper_v2,vnedge_algo_ml_pro_v1,stealth_trail_bbp_v1 \
  --lookback-days 30 \
  --profile paper_probe_candidate \
  --max-candidates 40 \
  --output research/live_research/context_scalper_v2_backtest_latest.json \
  --feed research/live_research/context_scalper_v2_backtest_feed.jsonl \
  --progress research/live_research/context_scalper_v2_backtest_progress.json \
  --heartbeat-seconds 30 --json
```

Promotion remains unchanged: a positive discovery result must still survive the
usual untouched-window judgment before it can become a governed paper lane.
