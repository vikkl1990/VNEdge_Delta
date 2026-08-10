# DATrend NomadaScalper Proxy Backtest

Date: 2026-07-22  
Source URL: https://www.tradingview.com/script/5G7J07WB-DATrend-Suite-Dots-NomadaScalper/  
VNEDGE scanner: `datrend_nomada_scalper_v1`

## Provenance

TradingView marks the referenced script as protected source. VNEDGE therefore
does **not** copy or execute Pine code for this test. The scanner is a
VNEDGE-owned proxy built from the public description:

- cycle-band oscillator stretch and reclaim/rejection;
- golden-marker arming followed by fast/slow timing cross;
- structure, daily cloud, volatility percentile, and ER-memory context gates;
- three-dot context panel approximating location, candle pressure, and momentum;
- structural stop plus fixed-R target, routed through maker/taker fee truth.

This is research-only. It cannot trade, promote, or bypass untouched-window
judgment.

## VM Replay

Commit tested: `c34baad`  
VM data root: `/home/ubuntu/vnedge/data`  
Runtime path: temporary worktree `/tmp/vnedge-datrend-c34baad`

Command:

```bash
python -m vnedge.research.scanner_tournament \
  --data-root data \
  --exchanges binanceusdm,bybit,delta_india \
  --symbols BTC/USDT:USDT,ETH/USDT:USDT,SOL/USDT:USDT,XRP/USDT:USDT,BNB/USDT:USDT,DOGE/USDT:USDT \
  --timeframes 5m,15m,1h \
  --strategies datrend_nomada_scalper_v1 \
  --lookback-days 30 \
  --profile discovery_relaxed \
  --max-candidates 100
```

Matrix: 3 venues x 6 pairs x 3 timeframes, 30-day lookback.  
Elapsed: 2m 57s.

## Results

Summary:

| Metric | Value |
|---|---:|
| Opportunities | 100 |
| Routed | 100 |
| Route mix | 100% maker |
| Candidate groups | 28 |
| Discovery watchlists | 5 |
| Strict watchlists | 0 |
| Can trade / promote | false / false |

Top discovery rows:

| Rank | Venue | Pair | TF | Verdict | Routed | Avg net bps | PF | Win % |
|---:|---|---|---|---|---:|---:|---:|---:|
| 1 | Binance | BNB/USDT | 15m | NEEDS_MORE_SAMPLES | 1 | +101.59 | 999.00 | 100.0 |
| 2 | Delta India | ETH/USD | 5m | NEEDS_MORE_SAMPLES | 1 | +52.68 | 999.00 | 100.0 |
| 3 | Binance | BTC/USDT | 15m | NEEDS_MORE_SAMPLES | 1 | +51.06 | 999.00 | 100.0 |
| 4 | Bybit | BTC/USDT | 15m | NEEDS_MORE_SAMPLES | 1 | +50.62 | 999.00 | 100.0 |
| 5 | Bybit | DOGE/USDT | 5m | DISCOVERY_WATCHLIST | 5 | +9.00 | 2.00 | 60.0 |
| 7 | Binance | BNB/USDT | 5m | DISCOVERY_WATCHLIST | 6 | +7.29 | 1.71 | 50.0 |
| 9 | Bybit | BNB/USDT | 5m | DISCOVERY_WATCHLIST | 6 | +6.07 | 1.50 | 50.0 |
| 10 | Binance | DOGE/USDT | 5m | DISCOVERY_WATCHLIST | 5 | +2.57 | 1.29 | 60.0 |
| 11 | Delta India | BTC/USD | 5m | DISCOVERY_WATCHLIST | 7 | +0.60 | 1.03 | 42.9 |

## Verdict

The proxy found real-looking pockets, but it does **not** break the production
fee wall yet:

- no group reached the strict evidence bar;
- the largest wins are one-trade samples and are not statistically usable;
- the 5m watchlists are positive only under relaxed discovery thresholds;
- maker routing is required; there is no taker-ready proof.

Best next step: keep `datrend_nomada_scalper_v1` in scanner tournaments and
feed its opportunities into the edge model. Do not add it to paper/live lanes
until a fresh run reaches at least 20 trades with PF > 1.5 and expected net
edge > 25 bps on untouched data.
