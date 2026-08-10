# Freqtrade Deep Dive

Snapshot date: 2026-07-09.

Source: [freqtrade/freqtrade](https://github.com/freqtrade/freqtrade),
default branch `develop`.

Freqtrade is a mature, open-source Python crypto trading bot with strong
research and operator tooling: dry-run, backtesting, hyperopt, pairlists,
protections, FreqAI, REST/Telegram control, FreqUI, and broad CCXT-based
exchange support.

For VNEDGE, the correct conclusion is not "replace our execution stack with
Freqtrade." The correct conclusion is: borrow its research ergonomics,
universe filtering, protection vocabulary, diagnostics, and model lifecycle
patterns while keeping VNEDGE's safety-first execution spine.

## Executive Verdict

Freqtrade is excellent for candle-driven strategy research and dry-run
operations. It is not a native L2/tick scalper execution engine.

What VNEDGE should borrow:

- chainable pair/universe filters
- dry-run and paper-first discipline
- explicit protections and pair locks
- lookahead and recursive-bias diagnostics
- orderflow candle aggregates from public trades
- rich backtest reporting by pair, tag, exit reason, and metric
- FreqAI-style feature expansion and model expiration
- producer/consumer-style research fanout
- FreqUI-style operator workflows

What VNEDGE should not copy:

- candle-close strategy semantics for sub-second scalping
- force-entry control routes
- broad exchange sprawl before execution proof
- hyperopt promotion without untouched-data governance
- live-capital controls that bypass VNEDGE's `PreTradeRiskGateway`
- source code directly, unless GPLv3 compatibility is explicitly reviewed

## Current Repo Read

As of this review, GitHub reports:

- repo: `freqtrade/freqtrade`
- description: free, open-source crypto trading bot
- default branch: `develop`
- language: Python
- license: GPL-3.0
- topics: `python`, `cryptocurrencies`, `trading-bot`,
  `algorithmic-trading`, `freqtrade`
- supported futures exchanges in docs include Binance, Bybit, Bitget, Gate,
  Hyperliquid, Kraken Futures, and OKX

Delta Exchange India is not a first-class supported venue in the inspected
Freqtrade docs. That matters for VNEDGE because Delta India remains one of the
target venues.

## Architecture

Freqtrade is organized around a single bot loop and a plugin-heavy research
surface.

High-level flow:

```text
CLI/config
-> ExchangeResolver
-> StrategyResolver
-> DataProvider
-> PairListManager
-> ProtectionManager
-> FreqtradeBot loop
-> Strategy callbacks
-> Exchange adapter / persistence / RPC
```

The core live loop:

1. Load open trades from persistence.
2. Refresh tradable pairlist.
3. Download complete OHLCV candles for whitelist and informative pairs.
4. Run `bot_loop_start()`.
5. Analyze each pair with:
   - `populate_indicators()`
   - `populate_entry_trend()`
   - `populate_exit_trend()`
6. Update open order state from exchange.
7. Process exits:
   - stoploss
   - ROI
   - exit signal
   - `custom_exit()`
   - `custom_stoploss()`
   - `confirm_trade_exit()`
8. Process position adjustment.
9. Process new entries:
   - entry signal
   - entry price
   - leverage callback
   - stake callback
   - `confirm_trade_entry()`

This is very good for candle bots. It is not a sub-second order-book event
loop.

## Strategy Interface

Freqtrade strategies are Python classes based on `IStrategy`.

Important fields and callbacks:

- `timeframe`
- `minimal_roi`
- `stoploss`
- `can_short`
- `order_types`
- `order_time_in_force`
- `startup_candle_count`
- `protections`
- `populate_indicators()`
- `populate_entry_trend()`
- `populate_exit_trend()`
- `custom_stake_amount()`
- `custom_entry_price()`
- `custom_exit_price()`
- `check_entry_timeout()`
- `check_exit_timeout()`
- `custom_stoploss()`
- `custom_exit()`
- `adjust_trade_position()`
- `leverage()`
- `order_filled()`

VNEDGE lesson:

We already have a stricter signal-to-order spine. The useful pattern is not the
exact interface; it is the clear separation between vectorized signal creation,
per-trade callbacks, and operational callbacks.

Recommended VNEDGE adaptation:

```text
SignalFamily
-> FeatureBuilder
-> CandidateBuilder
-> ExitPlanBuilder
-> RoutePolicy
-> ReplayPolicy
-> ShadowPolicy
```

This keeps scalper hypotheses structured without letting strategy code touch
execution.

## Pairlists And Universe Selection

Freqtrade's pairlist system is one of its strongest patterns. Pairlists can be
chained, and each handler either selects, filters, sorts, or modifies the
tradable universe.

Useful handlers inspected:

- `StaticPairList`
- `VolumePairList`
- `PercentChangePairList`
- `ProducerPairList`
- `RemotePairList`
- `MarketCapPairList`
- `CrossMarketPairList`
- `AgeFilter`
- `DelistFilter`
- `PerformanceFilter`
- `PrecisionFilter`
- `PriceFilter`
- `SpreadFilter`
- `RangeStabilityFilter`
- `VolatilityFilter`

VNEDGE should build the scalper equivalent. Our version should not be a generic
pairlist; it should be a lane allocator for recorder, replay, shadow, and paper
capacity.

Recommended VNEDGE filters:

| VNEDGE filter | Purpose |
|---|---|
| `VenueEnabledFilter` | Only Binance, Bybit, Delta India lanes currently configured. |
| `DerivativeContractFilter` | Perps/futures only, active contracts only. |
| `QuoteAssetFilter` | USDT, USDC, USD depending on venue. |
| `VolumeFilter` | Avoid dead markets. |
| `SpreadFilter` | Avoid markets where fee wall plus spread is impossible. |
| `DepthFilter` | Require top-N liquidity before recorder/shadow allocation. |
| `PrecisionFilter` | Avoid symbols where tick/step rounding destroys tight stops. |
| `VolatilityFilter` | Avoid dead/stable markets and extreme chaos unless explicitly assigned to event lanes. |
| `FundingRegimeFilter` | Mark funding-dislocation candidates. |
| `LeadLagFilter` | Mark cross-venue follower candidates. |
| `RecorderCoverageFilter` | Require enough tick/L2 coverage before replay. |
| `ReplayPerformanceFilter` | Prefer lanes with positive after-cost replay evidence. |
| `ShadowPerformanceFilter` | Prefer lanes with honest missed-opportunity and fill evidence. |
| `CooldownFilter` | Pause recently toxic lanes. |

This directly attacks the current "no signals" issue: the system should
allocate research attention to markets where a signal can realistically survive
spread, fees, depth, and fill probability.

## Protections

Freqtrade protections temporarily lock pairs or all trading after adverse
conditions.

Important protections:

- `StoplossGuard`
- `MaxDrawdown`
- `LowProfitPairs`
- `CooldownPeriod`

VNEDGE already has stronger risk invariants, including daily loss gates,
kill-switch behavior, reconciliation fail-closed behavior, and live-mode
gates.

Useful VNEDGE adaptation:

- Pair-side locks, not just pair locks.
- Lane locks by rejection type:
  - cost wall
  - toxic fill
  - no fill
  - stale data
  - adverse selection
  - replay decay
- Protection telemetry in the cockpit:
  - what is locked
  - why it is locked
  - when it can be reconsidered
  - whether unlock requires human approval

## Orderflow

Freqtrade has an experimental public-trade orderflow feature. It consumes raw
trades and exposes candle-level footprint data:

- raw trades per candle
- bid/ask volume
- delta
- min/max delta
- total trades
- price-bin footprint
- stacked imbalances

This is relevant to VNEDGE, but it sits between candle signals and full L2
replay. It does not model order-book queue position.

VNEDGE should add an orderflow feature lane:

```text
public trades
-> footprint bars
-> CVD / delta impulse / stacked imbalance / absorption proxy
-> context split by 4h / 1h / 15m / 1m
-> alpha factory event families
-> conservative replay if paired with L2 data
```

This is a practical bridge. It can improve daily scalper scanning without
pretending that footprint candles prove maker fills.

## Backtesting And Hyperopt

Freqtrade backtests include fees and produce rich reports:

- pair-level results
- entry tag stats
- exit reason stats
- mixed tag stats
- profit factor
- expectancy
- Sharpe / Sortino / Calmar
- drawdown
- rejected entry signals
- timeouts

Hyperopt uses Optuna-style parameter search. Freqtrade's docs make an important
distinction between "guards" and "triggers":

- guards are context filters
- triggers are event moments

This maps well to VNEDGE:

```text
4h / 1h / 15m context = guards
1m / orderflow / L2 event = trigger
maker/taker route = execution gate
```

VNEDGE must keep stricter promotion rules than ordinary hyperopt:

- no promotion on already-seen data
- no auto-variant can become paper/live without untouched judgment
- no parameter search directly updates a running strategy
- replay/paper/live must preserve the same costs and route assumptions

## Bias Diagnostics

Freqtrade includes two standout diagnostics:

- `lookahead-analysis`
- `recursive-analysis`

Lookahead analysis detects strategies that accidentally use future data.
Recursive analysis checks indicator instability from insufficient startup
history.

VNEDGE needs the same class of tooling for:

- generated strategy proposals
- feature builders
- multi-timeframe merges
- orderflow aggregates
- FreqAI-style model features
- Alpha Council suggested variants

Recommended VNEDGE build:

```text
research_bias_auditor
-> causality check
-> warmup/stability check
-> shifted-target leakage check
-> MTF alignment check
-> report usable by Alpha Council
```

This should be mandatory before any generated strategy reaches replay.

## FreqAI

FreqAI is a strong pattern for adaptive research, not a guarantee of edge.

Important ideas:

- train models per pair
- expand features across timeframes
- include correlated pairs
- include shifted candles
- adaptive retraining in dry/live
- model expiration
- save predictions
- reuse saved backtest predictions for threshold studies
- outlier removal
- dimensionality reduction
- producer/consumer fleet mode

Important caveats:

- dynamic volume pairlists do not work cleanly with FreqAI because FreqAI needs
  training data prepared ahead of time
- continual learning is experimental and can overfit or get stuck
- feature changes require a new model identifier
- model predictions are not trade permission

VNEDGE adaptation:

```text
FeatureRegistry
-> ModelTrainingQueue
-> ModelRegistry
-> PredictionStore
-> ModelAgeGate
-> DriftGate
-> AlphaCouncilReview
-> Replay/Judgment/Paper ladder
```

For scalping, FreqAI-style features should be used to discover conditional
edge, not to fire orders directly.

Recommended feature expansion:

- timeframes: `4h`, `1h`, `15m`, `1m`
- correlated pairs: BTC, ETH, SOL, BNB, XRP, DOGE majors by venue
- venue spreads: Binance vs Bybit vs Delta India
- shifted candles: last 1-8 bars depending on timeframe
- orderflow: delta, CVD slope, stacked imbalance, trade count burst
- L2: top-of-book imbalance, depth decay, microprice, queue pressure
- funding: funding percentile and funding shock
- session/time: UTC hour, India market overlap, funding windows

## Producer/Consumer Mode

Freqtrade's producer/consumer mode broadcasts analyzed dataframes and pairlists
from one instance to consumer instances.

VNEDGE equivalent should be:

```text
research producers
-> normalized research artifacts
-> Alpha Council
-> Alpha Workbench
-> dashboard
-> shadow/paper candidates
```

This is already the direction of VNEDGE. The missing upgrade is artifact
freshness and per-producer health shown in the UI:

- last produced time
- row count
- lane count
- stale/not stale
- next task
- latest candidate
- latest blocker

## FreqUI

FreqUI is strong for:

- bot state
- open trades
- profit/loss
- balance
- backtests from UI
- pairlist testing
- plots and plot configuration
- settings

VNEDGE should not copy FreqUI's shape exactly because our cockpit is different:
we need a quant operator terminal, not a retail bot control panel.

What to borrow:

- backtest result browser
- strategy diagnostics run from UI
- pairlist/lane test view
- model/training status
- per-pair performance
- wallet/equity view
- log tail

What to reject:

- internet-exposed control routes
- force entry/exit controls in the same operator UI
- UI as a command surface for live capital

VNEDGE cockpit principle:

```text
read-only first
evidence first
control only through audited operator workflows
```

## Why Freqtrade Does Not Solve Our Scalper Directly

Freqtrade deliberately works with complete candles in strategy dataframes. Its
docs warn that incomplete candles are not available because using them causes
repainting-style behavior.

That is correct for candle systems. It is not enough for the scalper we want.

Our scalper problem needs:

- live top-of-book and L2 state
- trade-through fill modeling
- queue position
- cancel/replace timing
- maker/taker route selection
- adverse-selection detection
- missed-opportunity logging
- fill probability by lane

Freqtrade can help us build the surrounding research and operations discipline,
but it should not be the final scalper execution model.

## VNEDGE Build Decisions

### Decision 1 - Do not replace VNEDGE execution

Keep the custom VNEDGE execution stack. It is safer and better aligned with:

- `PreTradeRiskGateway`
- three live gates
- kill switch
- idempotency journal
- reconciliation fail-closed behavior
- shadow/paper/live ladder
- Delta India requirement

### Decision 2 - Build Freqtrade-style scalper lane filters

The next practical build should be a chainable lane filter engine for all
exchange/symbol lanes.

Proposed module:

```text
vnedge.research.lane_filters
```

Output:

```text
research/live_research/lane_filter_latest.json
```

Each lane should report:

- included/excluded
- priority
- filter pass/fail reasons
- spread percentile
- depth score
- volatility score
- recorder coverage
- replay state
- shadow state
- last edge estimate
- next action

### Decision 3 - Add orderflow footprint features

Build trade-derived footprint features from recorded public trades.

This gives the Alpha Factory a middle layer between candles and full L2:

- trade delta
- CVD slope
- price-bin imbalance
- stacked imbalance proxy
- trade-count burst
- absorption proxy
- sweep proxy

These are still research features. They do not bypass L2 replay.

### Decision 4 - Add VNEDGE lookahead/recursive diagnostics

Every generated strategy or feature family should pass:

- no future shift leakage
- no MTF lookahead
- no unstable warmup dependency
- no target leakage
- no non-causal rolling aggregate

This is especially important if we let agents generate features.

### Decision 5 - Add model lifecycle telemetry

FreqAI's model age and retraining queue are useful.

VNEDGE should show:

- active model id
- model age
- training window
- prediction freshness
- drift score
- OOS score
- replay score
- can_trade=false until promoted

## Recommended PR Sequence

### PR 1 - Scalper Lane Filters

Branch:

```text
codex/scalper-lane-filters
```

Build:

- chainable lane filter contracts
- volume/spread/depth/precision/volatility/coverage filters
- latest JSON artifact
- Alpha Council intake row
- dashboard lane-filter panel

Why first:

This directly addresses "why are no signals firing?" by proving whether the
universe is tradable before signal logic is blamed.

### PR 2 - Orderflow Footprint Miner

Branch:

```text
codex/orderflow-footprint-miner
```

Build:

- public-trade footprint bars
- CVD and delta features
- stacked-imbalance proxy
- 1m/5m/15m context exports
- alpha factory family inputs

Why second:

It gives us richer scalper triggers without pretending we have maker-fill edge.

### PR 3 - Research Bias Auditor

Branch:

```text
codex/research-bias-auditor
```

Build:

- causality check
- MTF alignment check
- warmup/recursive stability check
- target leakage check
- generated strategy audit report

Why third:

Agent-generated alpha is dangerous without automatic leakage detection.

### PR 4 - Model Lifecycle And Expiration

Branch:

```text
codex/model-lifecycle-telemetry
```

Build:

- model age gate
- prediction freshness artifact
- drift/decay telemetry
- dashboard model lifecycle panel
- Alpha Council stale-model tasks

Why fourth:

It turns FreqAI's strongest operational idea into VNEDGE-safe governance.

## Bottom Line

Freqtrade is a mature candle-bot research and operations platform. Its best
ideas for VNEDGE are not the indicators. They are:

- universe filtering
- dry-run discipline
- protection locks
- bias diagnostics
- orderflow enrichment
- adaptive model lifecycle
- operator-visible research results

For our crypto daily scalping goal, the highest-value adoption is:

```text
Freqtrade-style lane filtering
+ orderflow footprint features
+ VNEDGE L2 replay
+ maker route economics
+ Alpha Council governance
```

That combination moves us toward a real edge factory without weakening the
capital-protection architecture.
