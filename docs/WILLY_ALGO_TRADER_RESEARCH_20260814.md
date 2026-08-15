# WillyAlgoTrader script research audit — 2026-08-14

Source profile: <https://in.tradingview.com/u/WillyAlgoTrader/#published-scripts>

## Scope and provenance

- Profile inventory at capture time: **40 published scripts**.
- Source access: **38 open-source**, **1 protected**, **1 invite-only**.
- Source-level audit completed for all 38 open-source scripts.
- Every accessible source was Pine v6 and declared `indicator(...)`; none declared
  `strategy(...)` or submitted a `strategy.entry/exit/order`.
- Third-party Pine source is **not vendored** into VNEDGE. This document stores
  public URLs, source characteristics, and independently worded research lessons.
- Public descriptions may claim non-repainting or report chart statistics. VNEDGE
  does not accept those claims as execution evidence. Every hypothesis still needs
  next-available entry, stop-first ambiguity, Delta costs, chronological selection,
  and a sealed untouched evaluation.

## Source audit summary

| Check | Result | VNEDGE meaning |
|---|---:|---|
| Open-source scripts audited | 38 | Mechanics can be inspected and clean-room tested |
| Executable TradingView strategies | 0 | Published win rates are indicator-side simulations, not broker-emulator evidence |
| Scripts using `request.security*` | 17 | Higher-timeframe availability must be reimplemented explicitly |
| Scripts using `barmerge.lookahead_on` | 16 | Safe only when the requested expression is correctly lagged; requires expression-level audit |
| Scripts using confirmed pivots | 19 | Pivot value is delayed knowledge, not knowledge at the pivot timestamp |
| Visible-chart-range dependent | 1 | Not reproducible in headless replay; reject as a signal feature |
| Scripts with no `barstate.isconfirmed` guard | 3 | Not automatically invalid, but irreversible state transitions need separate review |

The main reusable lesson is architectural: most scripts combine one primary
hypothesis with context, scoring, and attractive trade-plan drawings. VNEDGE must
test the primary hypothesis alone before adding the score or visual management.

## Complete catalog and disposition

| # | Script | Primary family | Source / causal observation | VNEDGE disposition |
|---:|---|---|---|---|
| 1 | [STRAT Trap & VWAP Engine](https://in.tradingview.com/script/ngb1s23R-STRAT-Trap-VWAP-Engine-WillyAlgoTrader/) | Failed-break trap + regime | Open source; pivots and 4 MTF reads; FTC includes a forming-HTF option | **Already audited. Retire standalone.** Causal 30m: −14.38 bps/trade, PF 0.207 |
| 2 | [Elliott Impulse Engine](https://in.tradingview.com/script/eQPdoeM6-Elliott-Impulse-Engine-WillyAlgoTrader/) | Wave-state projection | Open source; 4 confirmed pivots; 1,419-line state machine; no orders | Context/education only; projected path is not an entry edge |
| 3 | [Trader Assistant Pro](https://in.tradingview.com/script/ZoccxbOB-Trader-Assistant-Pro-WillyAlgoTrader/) | 1-2-3 reversal assistant | Invite-only; description available, source unavailable | Do not port or infer hidden rules |
| 4 | [Reactive Trail System](https://in.tradingview.com/script/73fIFFEV-Reactive-Trail-System-WillyAlgoTrader/) | Adaptive trail | Open source; one lag-sensitive MTF read | Exit-family research only; not a new entry hypothesis |
| 5 | [Bitcoin Almanac](https://in.tradingview.com/script/Z4DiNsKb-Bitcoin-Almanac-WillyAlgoTrader/) | Macro cycle model | Open source; fitted to roughly three completed BTC cycles | BTC context only; unsuitable for ETH intraday alpha |
| 6 | [Liquidity Trail Matrix](https://in.tradingview.com/script/cdnBBG0A-Liquidity-Trail-Matrix-WillyAlgoTrader/) | ATR trail + profile retest | Open source; MTF + candle-distributed volume profile | Decompose; profile can be context, trail can be exit logic |
| 7 | [Mirage Liquidity Sweep Pro](https://in.tradingview.com/script/qBUHu6aW-Mirage-Liquidity-Sweep-Pro-WillyAlgoTrader/) | Swing sweep + CHoCH | Open source; confirmed-pivot delay; MTF read | Candidate only as a low-frequency clean-room sweep contract |
| 8 | [Meridian Flow](https://in.tradingview.com/script/uGVA7N7G-Meridian-Flow-WillyAlgoTrader/) | BOS/CHoCH + order block | Open source; pivot-confirmed structure; MTF read | Structure metadata; old hierarchical BOS family already failed standalone |
| 9 | [Synapse Trail Pro](https://in.tradingview.com/script/RjkDXQnZ-Synapse-Trail-Pro-WillyAlgoTrader/) | ATR trail + quality score | Open source; lagged HTF read; 1,717 lines | Correlated trail-family member; do not test as an independent alpha count |
| 10 | [Volume-Weighted S/R Zones](https://in.tradingview.com/script/H0DfZXtR-Volume-Weighted-S-R-Zones-WillyAlgoTrader/) | Pivot S/R quality | Open source; pivot delay; candle volume proxy | Useful level-quality feature, not a primary scanner |
| 11 | [Liquidity Pools Pro](https://in.tradingview.com/script/UWC4kQ9O-Liquidity-Pools-Pro-WillyAlgoTrader/) | Equal-high/low pool sweep | Open source; confirmed pivots + MTF context | **Selection candidate:** pool formation → sweep → close-back, standalone first |
| 12 | [Adaptive Fibonacci Trailing System](https://in.tradingview.com/script/6YcVIGsL-Adaptive-Fibonacci-Trailing-System-WillyAlgoTrader/) | Adaptive trail + fib zones | Open source; confirmed pivots; no MTF | Exit/context family only |
| 13 | [Nexus Fusion Engine ML](https://in.tradingview.com/script/DRgzod5I-Nexus-Fusion-Engine-ML-WillyAlgoTrader/) | Momentum fusion + KNN-like score | Open source; 2 MTF reads; no confirmed-bar guard | Treat as heuristic feature engineering, not validated ML |
| 14 | [Self-Aware Trend System](https://in.tradingview.com/script/sXFWVpmg-Self-Aware-Trend-System-WillyAlgoTrader/) | Adaptive SuperTrend | Open source; pivot context; 36 alerts | Same economic family as rejected StealthTrail; low incremental value |
| 15 | [ABCD Harmonic Projection](https://in.tradingview.com/script/hNY1hl36-ABCD-Harmonic-Projection-WillyAlgoTrader/) | Harmonic completion | Open source; pivot-confirmed pattern | Exploratory only; high multiple-testing and low-sample risk |
| 16 | [Trade Strategy Calculator](https://in.tradingview.com/script/Zc1gOfeX-Trade-Strategy-Calculator-WillyAlgoTrader/) | Sizing / risk calculator | Open source; indicator with no market entries | Borrow risk-report concepts only; not a scanner |
| 17 | [Pulse Trend Radar](https://in.tradingview.com/script/qieolCzm-Pulse-Trend-Radar-WillyAlgoTrader/) | KAMA trend + retest | Open source; pivot zones | Trail/retest family; lower priority than Phantom clean-room test |
| 18 | [Breakout Pattern Setup](https://in.tradingview.com/script/MaFckffw-Breakout-Pattern-Setup-WillyAlgoTrader/) | Converging-channel breakout | Open source; pivot geometry and measured move | Candidate after compression breakout, with strict sample controls |
| 19 | [Daily Volume Profile Pro](https://in.tradingview.com/script/OjoifMMY-Daily-Volume-Profile-Pro-WillyAlgoTrader/) | Session volume profile | Open source; distributes candle volume across price bins | Context-only approximation; not event-level volume-at-price |
| 20 | [Fibonacci Structure Engine](https://in.tradingview.com/script/d7i2KmxR-Fibonacci-Structure-Engine-WillyAlgoTrader/) | BOS/CHoCH + fib entry | Open source; pivot-confirmed structure | Decompose into level metadata; do not port monolith |
| 21 | [StealthTrail SuperTrend ML Pro](https://in.tradingview.com/script/K0GnwBci-StealthTrail-SuperTrend-ML-Pro-WillyAlgoTrader/) | Adaptive SuperTrend + heuristic score | Open source; MTF uses lookahead-off; “ML” disabled by default | **Already audited. Retire standalone.** Causal 30m: −14.65 bps/trade, PF 0.268 |
| 22 | [Adaptive Ichimoku Nexus](https://in.tradingview.com/script/H7k5sk0Y-Adaptive-Ichimoku-Nexus-WillyAlgoTrader/) | Adaptive Ichimoku | Open source; 4 MTF reads and shifted cloud semantics | Swing context only; requires exact availability mapping |
| 23 | [Precision Sniper](https://in.tradingview.com/script/IZj18oYZ-Precision-Sniper-WillyAlgoTrader/) | 10-factor confluence vote | Open source; MTF read; no strategy orders | Composite agreement cannot manufacture edge; feature ablation only |
| 24 | [Smart Breakout Targets](https://in.tradingview.com/script/X6oAyAdo-Smart-Breakout-Targets-WillyAlgoTrader/) | BB/ATR compression breakout | Open source; 2 MTF reads | Merge into one canonical compression family; do not count as a separate discovery |
| 25 | [Adaptive Momentum Fusion](https://in.tradingview.com/script/Tq7S1S64-Adaptive-Momentum-Fusion-WillyAlgoTrader/) | Adaptive MACD family | Open source; no MTF; confirmed-close signals | Feature only; prior AMF-derived scanner family was rejected |
| 26 | [Swing Volume Profile Pro](https://in.tradingview.com/script/7yozLqCv-Swing-Volume-Profile-Pro-WillyAlgoTrader/) | Swing-leg volume profile | Open source; pivot delay; candle-range allocation | Context feature; explicitly not true traded volume-at-price |
| 27 | [Adaptive Spectral Forecast](https://in.tradingview.com/script/2GrMlaRY-Adaptive-Spectral-Forecast-WillyAlgoTrader/) | Harmonic spectral forecast | Open source; 5-harmonic default; stated overfit risk | Research diagnostic only; require purged walk-forward and placebo tests |
| 28 | [StealthTrail SuperTrend](https://in.tradingview.com/script/fdiiZPk6-StealthTrail-SuperTrend-WillyAlgoTrader/) | Filtered SuperTrend | Open source; no MTF | Same rejected trend-flip family; benchmark only |
| 29 | [Adaptive Momentum Classifier](https://in.tradingview.com/script/KLEBo02H-Adaptive-Momentum-Classifier-WillyAlgoTrader/) | Percentile momentum score | Open source; one MTF read | Feature-family candidate, not primary entry logic |
| 30 | [SmartTrend Pro](https://in.tradingview.com/script/9z4Y3HQj-SmartTrend-Pro-WillyAlgoTrader/) | Adaptive volatility trend | Open source at audit time; 5 MTF reads | Correlated trend family; low priority |
| 31 | [ICT Session Zones & Sweep Signals](https://in.tradingview.com/script/aRXsTqlH-ICT-Session-Zones-Sweep-Signals-WillyAlgoTrader/) | Session sweep | Open source; no MTF; confirmed close | Session context is usable, but prior VNEDGE session-sweep v1 did not clear costs |
| 32 | [Adaptive Trend Pro](https://in.tradingview.com/script/CeUI4gJs-adaptive-trend-pro-willyalgotrader/) | Adaptive trail | Protected; source unavailable | Description-only; no clean-room reconstruction from hidden rules |
| 33 | [Adaptive Squeeze Momentum Pro](https://in.tradingview.com/script/XhQiviEN-adaptive-squeeze-momentum-pro-willyalgotrader/) | Squeeze + normalized momentum | Open source; pivots used for divergence; 2 MTF reads | Confirmation feature for one canonical compression experiment |
| 34 | [Automatic Fibonacci Levels](https://in.tradingview.com/script/upj9fpww-automatic-fibonacci-levels-willyalgotrader/) | Visible-range fib grid | Open source; depends on `chart.left/right_visible_bar_time` | **Reject for replay.** Output changes with chart viewport |
| 35 | [Auto S/R Channels](https://in.tradingview.com/script/ceZQ8LOe-auto-s-r-channels-willyalgotrader/) | Best-fit pivot channel | Open source; pivot-confirmed; no alerts | Context/research geometry only |
| 36 | [Squeeze Breakout Pro](https://in.tradingview.com/script/9bwEi6EK-squeeze-breakout-pro-willyalgotrader/) | BB-inside-KC breakout | Open source; no MTF; confirmed close; explicit range risk | **Highest-priority standalone OHLCV hypothesis** |
| 37 | [Smart Money Engine](https://in.tradingview.com/script/9wnWcxkQ-smart-money-engine-willyalgotrader/) | BOS/CHoCH + OB/FVG | Open source; 1,638-line stateful monolith; MTF read | Decompose; do not port the full visual engine |
| 38 | [Adaptive Pivot Structure](https://in.tradingview.com/script/QhXQwVpN-adaptive-pivot-structure-willyalgotrader/) | Pivot structure + live fib | Open source; confirmed pivots but no explicit confirmed-bar guard | Context only pending state-transition audit |
| 39 | [Adaptive Volatility Trend](https://in.tradingview.com/script/CFgnJqe7-adaptive-volatility-trend-willyalgotrader/) | ER-adaptive ATR trend | Open source; no MTF | Simple trend benchmark; same correlated family |
| 40 | [Phantom Trend Cloud](https://in.tradingview.com/script/5LGQFStB-phantom-trend-cloud-willyalgotrader/) | Trend-cloud retest | Open source; safe-claim HTF pattern must still be reproduced exactly | **Secondary candidate:** 15m+ retest, standalone before scoring |

## Normalized families — avoid false diversification

Forty names collapse to a much smaller hypothesis set:

1. **Adaptive trend/trail:** Reactive Trail, LTM, Synapse, AFT, SATS,
   Pulse, both StealthTrails, SmartTrend, Adaptive Trend, AVT, Phantom.
2. **Structure/liquidity:** STRAT Trap, Mirage, Meridian, Volume S/R,
   Liquidity Pools, Fibonacci Structure, Smart Money, APS, ICT Sessions.
3. **Compression/breakout:** Smart Breakout Targets, Squeeze Breakout,
   Adaptive Squeeze Momentum, Breakout Pattern Setup.
4. **Momentum/confluence:** Nexus, AMF, Adaptive Momentum Classifier,
   Precision Sniper, Adaptive Ichimoku.
5. **Projection/context:** Elliott, ABCD, profiles, Fibonacci grids,
   Bitcoin Almanac, Spectral Forecast, Trade Calculator.

Testing every title as an independent strategy would badly understate multiple
comparisons because scripts within a family share the same economic bet.

## Three justified next experiments

### 1. `squeeze_breakout_willy_cleanroom_v1`

- Closed 15m or 1h decision candle.
- Bollinger Bands inside Keltner Channel for a frozen minimum duration.
- Range high/low frozen before the breakout bar.
- Confirmed close beyond the frozen range with volume and normalized momentum.
- Entry at next bar open; structural stop at the opposite range edge.
- Reject unless target distance is at least 5× configured Delta costs.
- One selection run, then one sealed untouched evaluation.

### 2. `liquidity_pool_sweep_cleanroom_v1`

- Equal-high/equal-low pool formed only from already-confirmed pivots.
- Pool timestamp is the pivot **confirmation** time, not the pivot's plotted bar.
- Entry hypothesis: sweep beyond pool, confirmed close back inside, next-open fill.
- Standalone economics first; CHoCH/HTF score only after positive selection evidence.

### 3. `phantom_cloud_retest_cleanroom_v1`

- 15m/1h only; Hull-weighted midline and ATR cloud.
- Trend established before retest; first rejection after a pullback into the cloud.
- Freeze volume/RSI/ADX gates in a preregistered contract.
- Evaluate as a swing/retest setup; do not force it into the 30-minute scalper label.

## Correction to the attached “production-ready” engine

The attached engine must not be used as evidence without repair. The pasted code
contains syntax corruption and material research errors:

- `from future import annotations`, `def init`, and the main guard are malformed.
- `hit_stop = low = self.stop_price` assigns instead of comparing, making the
  long stop path truthy; the short exit branch is missing.
- A long position reads `exit_short` instead of `exit_long`.
- Signals fill at the signal close instead of the next available open.
- The centered rolling swing detector uses future bars and therefore looks ahead.
- Trade-level `net_pnl/net_bps` omit the entry fee even though equity pays it.
- `bars_held` is elapsed minutes, not bars, and becomes wrong on non-1m data.
- Same-bar stop/target ambiguity, gap-through fills, funding, lot size, and minimum
  notional are not modeled.
- The report promotes on the same data used to choose the rules; there is no
  chronological selection/validation or sealed untouched proof.

VNEDGE's existing causal replay and fee-wall evidence path should remain the
evaluation authority. The attached engine is a sketch, not a production backtester.

## Decision

Do not copy all 40 indicators into scanners. Preserve the catalog, retire the two
already-disproved families, and test only the three independent hypotheses above.
All resulting lanes remain research-only with `can_trade=false`,
`can_promote=false`, and no order route until promotion evidence exists.
