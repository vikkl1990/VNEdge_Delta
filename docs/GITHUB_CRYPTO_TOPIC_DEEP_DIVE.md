# GitHub Crypto Topic Deep Dive

Snapshot date: 2026-07-09.

This review treats GitHub's `topics/crypto` page as an external intelligence
source for VNEDGE. The topic is broad: most high-ranked projects are wallets,
cryptography libraries, blockchains, or security tools. For VNEDGE, the useful
subset is much narrower:

- exchange connectivity
- market data normalization
- backtesting and replay
- market making and execution
- strategy research workflows
- agentic research orchestration
- operator UI and dry-run/paper controls

The conclusion is direct: the missing scalper edge is unlikely to come from
copying another indicator pack. The better lesson from mature open-source
crypto trading systems is that profitable automation is built around data
contracts, realistic execution simulation, route-aware costs, strategy
sandboxing, and aggressive rejection logging.

## Source Set

Primary references inspected from the crypto/trading ecosystem:

| Project | What matters for VNEDGE |
|---|---|
| [ccxt/ccxt](https://github.com/ccxt/ccxt) | Broad exchange abstraction and market discovery. Useful for symbol universe management, not enough for high-frequency execution quality by itself. |
| [bmoscon/cryptofeed](https://github.com/bmoscon/cryptofeed) | Exchange websocket feed normalization. Strong reference for multi-exchange public data lanes and backend fanout. |
| [hummingbot/hummingbot](https://github.com/hummingbot/hummingbot) | Market-making and HFT-oriented controller/connector architecture. Strong reference for maker quoting, inventory control, and cancel/replace loops. |
| [freqtrade/freqtrade](https://github.com/freqtrade/freqtrade) | Mature crypto strategy research, dry-run, pairlists, hyperopt, FreqAI, UI, and ops discipline. Better for candle strategies than sub-second scalping. |
| [jesse-ai/jesse](https://github.com/jesse-ai/jesse) | Strategy authoring, multi-timeframe backtesting, optimization, smart orders, risk metrics, and an agent-facing strategy workflow. |
| [nautechsystems/nautilus_trader](https://github.com/nautechsystems/nautilus_trader) | The best architectural reference: deterministic event-driven research/live parity, tick/order-book simulation, adapters, cache, portfolio, execution, and risk separation. |
| [QuantConnect/Lean](https://github.com/QuantConnect/Lean) | Institutional-style algorithm engine with research/live separation and broad asset support. Good model for testable strategy contracts. |
| [OpenBB-finance/OpenBB](https://github.com/OpenBB-finance/OpenBB) | Data platform for analysts, quants, and agents. Useful model for data access as a product surface. |
| [TauricResearch/TradingAgents](https://github.com/TauricResearch/TradingAgents) | Multi-agent debate framework for financial research. Useful for VNEDGE's Alpha Council, but not an execution model. |
| [HKUDS/Vibe-Trading](https://github.com/HKUDS/Vibe-Trading) | Agentic trading research with shadow/advisory workflows, alpha library, generated-code controls, and safety boundaries. |
| [CryptoSignal/Crypto-Signal](https://github.com/CryptoSignal/Crypto-Signal) | Classic technical-analysis signal bot. Useful as a cautionary comparison: signal breadth is not the same as executable edge. |

## What The Best Repos Converge On

### 1. Research/live parity beats dashboard optimism

Nautilus, Lean, Jesse, and Freqtrade all make the same architectural point:
research is only useful when the live path shares the same assumptions.

VNEDGE already has the right safety spine:

- strategy output is not an order
- every order goes through the risk gateway
- paper/shadow/live are separate rungs
- decisions are journaled
- replay is required before promotion

The gap is narrower but important: scalper replay must model the same maker
quote, queue, cancel, partial fill, timeout, and taker exit semantics that live
execution will use. If replay assumes a maker fill that live cannot get, the
alpha is fake.

Required build direction:

- one normalized event schema for candles, trades, book updates, funding, and
  venue status
- one order-intent schema across replay, paper, shadow, and live
- backtest/replay gateway policies aligned with operational gateway policies
- explicit queue-position and trade-through models for passive quotes

### 2. Market data is a product, not a helper function

Cryptofeed and CCXT show the split clearly:

- CCXT is useful for market discovery and REST normalization.
- Cryptofeed is the better mental model for websocket feed normalization.

For a scalper, the feed is the strategy input, not plumbing. VNEDGE needs lane
health as first-class data:

- exchange
- symbol
- channel
- sequence/checksum status where available
- staleness
- spread quality
- book depth availability
- trade/book event ratio
- coverage duration
- recorder gaps
- replay eligibility

If a lane is silent, the system should prove whether the reason is:

- no market data
- stale market data
- no quote-worthy setup
- quote did not fill
- fill was adverse
- cost wall blocked
- risk gate blocked
- route policy blocked
- trial state blocked

### 3. Market-making architecture is different from indicator architecture

Hummingbot's useful lesson is not "run Hummingbot." It is the separation of:

- connector
- controller
- quote proposal
- inventory/risk constraints
- execution adapter
- telemetry

That is the right shape for a VNEDGE scalper. A profitable scalp is usually not
"indicator says buy." It is closer to:

```text
context says long bias
microstructure says short-horizon pressure
spread/depth says quote is worth placing
queue/fill model says maker fill is plausible
adverse-selection model says filled quote is not toxic
exit model says loss can be cut fast enough
route policy says maker/taker economics clear the fee wall
```

This is why more TradingView-style confluence does not automatically create a
bot edge. Manual traders can skip, wait, and read context visually. A bot needs
the exact execution state encoded.

### 4. Agent systems are useful only as research governors

TradingAgents and Vibe-Trading are relevant because they treat agents as
research collaborators, not capital controllers. The valuable ideas are:

- role-separated debate
- structured output
- persistent decision logs
- reflection/memory from prior decisions
- source attribution
- checkpointed research tasks
- generated-code sandboxing
- narrow environment permissions

VNEDGE's Alpha Council is aligned with this. The next step is not "LLM trades."
The next step is:

- agents propose strategy hypotheses
- agents attack the hypothesis
- execution agent prices maker/taker viability
- risk agent vetoes weak evidence
- workbench turns surviving ideas into replay tasks
- promotion still requires untouched judgment and human approval

### 5. Mature bots expose why they did not trade

Freqtrade, Jesse, and Hummingbot have strong operational surfaces: dry-run,
logs, web UI, Telegram/alerts, status, and strategy diagnostics.

VNEDGE's current concern is not just "few signals." It is observability:

- show every lane's latest candidate count
- show every rejection bucket
- show the closest-to-trade candidate
- show the missing field that blocked it
- show route decision: `BLOCKED`, `MAKER_ONLY`, `TAKER_ALLOWED`
- show edge estimate after costs
- show fill assumption
- show whether the Alpha Council has created a next task

The operator UI must feel less like a static dashboard and more like a trading
terminal plus evidence courtroom.

## What Not To Copy

Do not copy these patterns into VNEDGE:

- Blind technical-indicator stacking without execution-cost proof.
- Alert bots that emit signals without fill, fee, slippage, and adverse
  selection accounting.
- LLM agents that can place or mutate live orders.
- Wide exchange support before one or two venues prove the full path.
- Hyperparameter search that can promote on already-seen data.
- "Win rate" marketing without profit factor, payoff, drawdown, and fill
  realism.

Also do not try to absorb entire engines. VNEDGE already has a safety-first
architecture. The right move is selective adoption of proven patterns.

## VNEDGE Gap Map

| Area | Current state | Gap | Reference pattern |
|---|---|---|---|
| Exchange discovery | CCXT-style discovery exists in research lanes | Needs durable per-exchange symbol registry with volume, spread, depth, fees, and recorder coverage | CCXT, Freqtrade pairlists |
| Public data lanes | Tick/L2 recorders exist | Need normalized feed contract and lane health manifest | Cryptofeed, Nautilus |
| Scalper replay | Conservative replay exists | Need queue-position, cancel/replace, partial fill, and maker adverse-selection modeling | Nautilus, Hummingbot |
| Route policy | Maker/taker route decision exists | Need live shadow runner to log route choice and missed maker opportunities every minute | Hummingbot |
| Alpha factory | Event-family mining exists | Need agent-generated hypotheses in a sandbox, then council review and replay queue | Vibe-Trading, Jesse, TradingAgents |
| Agent council | Research-only council exists | Needs persistent memory that learns from shadow/paper outcomes and rejected near-misses | TradingAgents, Vibe-Trading |
| Strategy authoring | Core strategies are code-level | Need safe proposal contract for generated strategies without core-source mutation | Jesse MCP pattern, Vibe sandbox |
| UI | Cockpit exists | Needs terminal-grade lane tape, rejection funnel, proof queue, and agent council live state | Jesse/Freqtrade/Hummingbot ops surfaces |
| Paper/shadow | Safety ladder exists | Need all scalper families routed through shadow with missed-opportunity accounting before paper | VNEDGE invariant plus Hummingbot-style telemetry |

## Build Plan

### Phase 1 - Crypto Topic Intelligence Registry

Create a maintained internal registry of external references. This prevents the
team from re-reviewing the same repos and keeps external inspiration cleanly
separated from VNEDGE implementation.

Deliverables:

- `docs/GITHUB_CRYPTO_TOPIC_DEEP_DIVE.md`
- source categories: data, execution, backtest, agents, UI
- copy/reject decision per category
- VNEDGE build mapping

Status: this document.

### Phase 2 - Normalized Feed Contract

Build the data foundation inspired by Cryptofeed and Nautilus.

Deliverables:

- `NormalizedTradeTick`
- `NormalizedBookTop`
- `NormalizedBookDelta` where venue data supports it
- `FundingSnapshot`
- `LaneHealth`
- `ReplayCoverageManifest`
- per-lane status file consumed by dashboard and Alpha Council

Acceptance:

- every Binance, Bybit, and Delta India lane reports coverage, staleness,
  event counts, spread percentiles, and replay eligibility
- missing data creates a workbench task, not silent signal drought
- replay cannot run on ambiguous lane data

### Phase 3 - Maker Scalper Execution Model

Build the execution proof layer before asking for more signals.

Deliverables:

- queue-position model
- maker quote TTL model
- cancel/replace simulator
- strict post-only assumption
- maker fill probability estimate
- adverse-selection score after fill
- partial-fill accounting
- route selector: blocked / maker-only / taker-allowed

Acceptance:

- every replay row states whether profit came from directional move, spread
  capture, or optimistic fill assumptions
- no route can pass without positive expectancy after fees and slippage
- taker entry requires a higher PF and net-bps floor than maker entry

### Phase 4 - Strategy Proposal Sandbox

Build the safe agentic research layer inspired by Jesse/Vibe-Trading.

Deliverables:

- `StrategyProposal` JSON contract
- allowed feature list
- allowed timeframe list: `4h`, `1h`, `15m`, `1m`, tick/L2
- AST validation for generated Python
- no network
- no secrets
- no file writes outside sandbox output
- generated strategy cannot import execution/risk/order modules
- output is replay candidate only

Acceptance:

- agents can propose and test hypotheses
- no generated code can place an order
- every proposal is reviewed by Alpha Council
- replay and untouched judgment remain mandatory

### Phase 5 - Signal Funnel Observability

Turn "no trades" into an explainable report.

Deliverables:

- per-lane minute log: candidates, rejects, closest candidate, route decision
- missed maker opportunities
- rejected taker opportunities
- cost-wall rejects
- no-fill rejects
- stale-data rejects
- Alpha Council task linkage
- dashboard terminal panel for the signal funnel

Acceptance:

- operator can answer "why no signal?" within one screen
- every lane has a latest reason and next action
- agent council state is visible live

## Scalper Implication

The strongest external lesson is that a scalper must be an execution-edge
machine, not a signal-alert machine.

For VNEDGE, the next profitable scalper candidate should be framed as:

```text
context filter
+ microstructure family
+ maker fill model
+ adverse-selection filter
+ exit intelligence
+ route economics
+ replay proof
+ shadow missed-opportunity proof
```

The candidate is not valid because it fires often. It is valid only if the
route selected by the bot has positive expectancy after the real fee wall.

## Recommended Next PR

The next code PR should be:

```text
codex/feed-contract-maker-sim
```

Scope:

1. Add normalized feed and lane-health contracts.
2. Add replay coverage manifests.
3. Add maker quote fill/queue/adverse-selection metrics.
4. Expose per-lane "why no trade" output for the dashboard and Alpha Council.

This is the highest-leverage build because it attacks the real blocker: the
bot does not need more raw indicators; it needs proof that a route can survive
fees, fills, and adverse selection.
