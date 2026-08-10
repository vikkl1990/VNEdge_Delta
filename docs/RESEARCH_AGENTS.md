# Research Agents And Multi-Exchange Lanes

VNEDGE's research agents are bounded, local assistants for the slow loop. They
rank profitable exchange/symbol lanes, explain rejected lanes, and propose
whitelisted exploratory variants. They do not place orders, promote strategies,
alter paper-trial params, or bypass untouched-data judgment.

## Run

```bash
RESEARCH_EXCHANGES=binanceusdm,bybit,delta_india \
RESEARCH_SYMBOLS=BTC/USDT:USDT,ETH/USDT:USDT,SOL/USDT:USDT \
.venv/bin/python -m vnedge.research.continuous_research
```

The live-data workspace also defaults to all three venues:

```bash
DASHBOARD_TOKEN=... .venv/bin/python -m vnedge.runtime.multi_lane_shadow
```

Binance and Bybit use websocket feeds and governed funding-MR paper/shadow
lanes. Delta India currently uses a public REST-polled, candle-only shadow lane
(`trend_continuation_v1`) because current CCXT exposes Delta India public REST data
but no CCXT Pro websocket feed and no funding-rate history. Funding-dependent
Delta India research rows are marked `UNTESTABLE` until a native historical funding
source is added. Default VNEDGE USDT perp symbols are mapped to Delta India's
USD-settled perps (`BTC/USDT:USDT` -> `BTC/USD:USD`) unless a
`RESEARCH_SYMBOLS_DELTA_INDIA` override is supplied.

Per-exchange symbol overrides are supported:

```bash
RESEARCH_EXCHANGES=binanceusdm,bybit \
RESEARCH_SYMBOLS=BTC/USDT:USDT,ETH/USDT:USDT \
RESEARCH_SYMBOLS_BYBIT=BTC/USDT:USDT,SOL/USDT:USDT \
.venv/bin/python -m vnedge.research.continuous_research
```

Optional strategy allowlists let operators split heavy candle lanes without a
code change:

```bash
RESEARCH_STRATEGIES=quant_signal_pack_v1,alpha_stack_confluence_v1 \
.venv/bin/python -m vnedge.research.continuous_research
```

Leave `RESEARCH_STRATEGIES` unset/empty to run every registered research lane.

## Output

`research/live_research/latest.json` now includes:

- `universe`: exchange/symbol/timeframe coverage for the cycle.
- `results`: exchange-aware walk-forward records.
- `edge_agents.profitable_pairs`: best currently profitable lane per
  exchange/symbol.
- `edge_agents.proposals`: exploratory follow-ups, including pre-registered
  judgment prompts, cross-exchange validation prompts, and whitelisted variant
  backtests.
- `edge_leaderboard`: fee-aware ranked rows across strategy and Quant-family
  lanes. It includes `route_decision` (`BLOCKED`, `MAKER_ONLY`,
  `TAKER_ALLOWED`), blockers, score, fee drag, and a `promotion_queue`.
- `scalper_research`: tick/L2 replay diagnostics and recorder targets.
  Includes `focus`, a scalper-specific readiness drilldown that explains why
  replay candidates are absent and which lanes deserve recorder/mining effort.
- `alpha_factory`: structural alpha hypotheses and replay queue. See
  `docs/ALPHA_FACTORY.md`. Hypotheses are split by `4h/1h/15m/1m` context
  tags when those candle datasets are available.
- `alpha_stack_confluence_v1`: causal candle-structure confluence lane. See
  `docs/ALPHA_STACK.md`.
- `quant_signal_pack_v1`: broader Lux/Willy-style concept pack covering
  structure, sweeps, FVG/order-block retests, squeeze release, VWAP reclaim,
  multi-horizon bias, displacement, and volume impulse. Its walk-forward rows
  include `family_attribution` so the agent can isolate carrying sub-patterns
  such as `liquidity_sweep` or `fvg_retest` instead of tuning the blended pack.
  See `docs/QUANT_SIGNAL_PACK.md`.
- `scalper_parameter_registry`: frozen TF/horizon, family, fee, route, and
  exit policy contract. See `docs/SCALPER_PARAMETERS.md`.
- `edge_agents.policy`: the hard safety policy. `can_trade=false`,
  `can_promote=false`, and untouched-data judgment remains required.

## Edge Leaderboard

The leaderboard is the bridge from "signals exist" to "which signal deserves
research capital next." It ranks:

- normal rolling walk-forward strategy rows;
- Quant Signal Pack family rows from `family_attribution`;
- auto-explore variants, explicitly marked as requiring human review.

Rows below net-positive after fees, minimum sample count, or maker PF are
`BLOCKED`. Rows that clear maker policy but not promotion are `WATCHLIST` or
`VARIANT_RESEARCH_READY`. Taker is only `TAKER_ALLOWED` when PF, payoff, and
fee coverage are materially stronger than the maker floor.

The `promotion_queue` never promotes or trades. Its allowed next steps are:

- `pre_register_untouched_judgment`;
- `human_review_auto_variant_then_pre_register`;
- `run_isolated_family_variant`;
- `collect_more_and_retest`.

## Scalper Scanners

Scalping uses a separate tick/L2 scanner because candles cannot prove
microstructure edge. The scanner is research-only and ranks lanes for recorder
and replay attention:

```bash
.venv/bin/python -m vnedge.research.scalper_scanners \
  --exchanges binanceusdm,bybit \
  --symbols BTC/USDT:USDT,ETH/USDT:USDT,SOL/USDT:USDT \
  --days YYYYMMDD
```

Full exchange-wide derivative discovery:

```bash
.venv/bin/python -m vnedge.research.scalper_scanners \
  --all-markets \
  --exchanges binanceusdm,bybit,delta_india \
  --quote-assets USDT,USDC,USD \
  --days YYYYMMDD \
  --limit 200
```

It emits:

- `recorder_priority`: which exchange/symbol lanes deserve more tick/L2 data.
- `edge_score`: how close replay evidence is to a research candidate.
- `route_decision`: `BLOCKED`, `MAKER_ONLY`, or `TAKER_ALLOWED`, based on PF
  and net bps after maker/taker/slippage costs.
- `state`: `MISSING_TICK_DATA`, `RECORD_MORE`, `REPLAY_CANDIDATE`, or the
  rejection class (`REJECTED_COST_WALL`, `REJECTED_NO_FILLS`, etc.).
- `recorder_targets`: unique exchange/symbol lanes to arm next.

Hard policy: `can_trade=false`, `can_promote=false`. A `REPLAY_CANDIDATE` still
requires pre-registered untouched replay before paper/shadow discussion.
Rows below PF/breakeven are `BLOCKED`; they are not weak signals.

## Scalper Focus

`scalper_research.focus` is the operator-facing drilldown for periods where
scalping is not firing. It summarizes:

- replay candidates and edge-hypothesis candidates;
- missing tick/L2 data and under-recorded lanes;
- cost-wall gaps (`avg_net_bps` and PF versus maker floors);
- top recorder campaign lanes;
- next actions such as `record tick/L2`, `run conservative replay`, or
  `do not trade`.

The focus report is not a signal. It always carries `can_trade=false` and
`can_promote=false`. A scalper only advances after conservative replay,
untouched judgment, and human approval.

## Scalper Edge Miner

Use the edge miner when scanners say "edge is missing." It searches recorded
tick/L2 data for microstructure hypotheses before strategy promotion:

```bash
.venv/bin/python -m vnedge.research.scalper_edge_miner \
  --all-markets \
  --exchanges binanceusdm,bybit,delta_india \
  --quote-assets USDT,USDC,USD \
  --days YYYYMMDD \
  --limit 100
```

It tests pressure continuation, absorption reversal, and microprice
continuation across forward horizons. Results are still research-only and use
the same route gate: below PF/breakeven means `BLOCKED`.

The 2026-07-05 replay sweep tombstoned the continuous
`book_imbalance_continuation` premise after all 120 configs lost after costs.
Do not recycle that shape as "almost working." Treat it as an audit baseline
unless a new premise is pre-registered.

## Alpha Factory

Use the alpha factory to mine structural hypotheses from recorded tick/L2 tape:

```bash
.venv/bin/python -m vnedge.research.alpha_factory --days YYYYMMDD --limit 100
```

It looks for forced-flow continuation, absorption reversal, microprice
dislocation, liquidity-vacuum continuation, and volatility impulse families.
Any positive result is a replay queue item, not a signal. Conservative replay,
untouched judgment, and human paper/shadow approval remain mandatory.

The factory also publishes `alpha_factory.tournament`, an event-family
tournament that ranks active scalper premises by exchange, symbol, context
tag, post-cost net bps, PF, route decision, and route gap. Tournament
`REPLAY_*_CANDIDATE` rows are replay work orders only; `BLOCKED_FEE_WALL`
rows must not be traded or parameter-rescued.

## Guardrails

- A rolling PASS is only a candidate signal.
- Positive rejected lanes can be ranked as interesting, but remain rejected.
- Agent variants come from `strategy_diagnostics.CATALOG`; no arbitrary search.
- Auto-explore records are marked `auto=true` and cannot be promoted directly.
- The running paper trial is never retuned by this loop.
- Scalper scanners guide data collection and replay only; they never route
  signals into execution.
- The edge miner creates hypotheses only; no mined edge can trade without
  untouched replay, paper, shadow, and gateway approval.
- The alpha factory creates structural hypotheses only; no hypothesis can trade
  without conservative replay, untouched judgment, paper, shadow, and gateway
  approval.
- The alpha tournament ranks replay work only; no tournament row is an
  executable signal.
- AlphaStack emits ordinary strategy intents only after walk-forward scoring;
  it is not an indicator-alert import path and cannot bypass promotion gates.
