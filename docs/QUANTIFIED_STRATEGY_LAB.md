# Quantified Strategy Lab

VNEDGE now tracks the operator-supplied QuantifiedStrategies 95-strategy title
inventory as a finite research queue.

This is deliberately **title-only research**. The pack list is useful because it
names proven research themes, but it does not give VNEDGE executable rules. The
lab does not copy paid or proprietary rules, does not emit trading signals, and
does not promote lanes. Each title becomes one of three things:

- a VNEDGE-owned crypto hypothesis,
- a crypto session/relative-strength study,
- or a quarantined asset-specific idea.

## How To Publish

```bash
python -m vnedge.research.quantified_strategy_lab
```

Default artifacts:

- `research/live_research/quantified_strategy_lab_latest.json`
- `research/live_research/quantified_strategy_lab_feed.jsonl`

Dashboard:

- `/quantified-strategy-lab`
- `/quantified-strategy-lab/kb`
- `/quantified-strategy-lab/port-factory`
- `/quantified-strategy-lab/blueprint-proof`
- `/quantified-strategy-lab/proof-arbiter`
- `/quantified-strategy-lab/pullback-proof` (legacy first-lane view)

All dashboard surfaces are read-only and dashboard-token gated.

## Port Factory

The port factory turns the title lab into durable Quant OS research tasks:

```bash
python -m vnedge.research.quantified_port_factory
```

Default artifacts:

- `research/live_research/quantified_port_factory_latest.json`
- `research/live_research/quantified_port_factory_feed.jsonl`

By default it queues Chunks A-D into the Quant OS agent gateway. Chunk Q remains
quarantine-only. Re-running the publisher reuses existing tasks by stable
blueprint id and writes a new content artifact only when the blueprint hash
changes.

## Blueprint Proof Matrix

The complete proof publisher turns the syncable build families into durable
Agent Gateway backtest cells:

```bash
python -m vnedge.research.quantified_blueprint_proof --seed-jobs
```

Default artifacts:

- `research/live_research/quantified_blueprint_proof_latest.json`
- `research/live_research/quantified_blueprint_proof_feed.jsonl`

The default matrix covers six blueprint families:

- `bitcoin_crypto_strategy_pack_v1`
- `range_volatility_breakout_reversion_v1`
- `pullback_reversion_pack_v1`
- `indicator_pack_mtf_v1`
- `crypto_session_calendar_miner_v1`
- `crypto_relative_strength_rotation_v1`

It evaluates 1m, 5m, 15m, 1h, and 4h where data exists. The paper profile is
fixed at 100 USD margin and 25x notional for evidence normalization only; the
publisher cannot create paper, shadow, or live orders. Session and
relative-strength ports are explicitly marked as proxy adapters until VNEDGE
has dedicated session-settlement and portfolio-rotation strategy classes.
Positive proxy results require a canonical VNEDGE port before any promotion
review.

## Result Arbiter

The proof matrix is intentionally raw evidence. The result arbiter converts
those cells into operator next actions:

```bash
python -m vnedge.research.quantified_proof_result_arbiter
```

Default artifacts:

- `research/live_research/quantified_proof_result_arbiter_latest.json`
- `research/live_research/quantified_proof_result_arbiter_feed.jsonl`

Action buckets:

- `READY_FOR_UNTOUCHED_JUDGMENT`: canonical row clears the proof gate.
- `PROXY_EDGE_NEEDS_CANONICAL_PORT`: proxy row clears math but needs a real
  session/rotation scanner before judgment.
- `EXTEND_SPARSE_POSITIVE`: edge is positive but sample is too small.
- `EXIT_ROUTE_UPLIFT` / `FEE_WALL_NEAR_MISS`: mine TP1/BE/trailing and
  maker/taker routing improvements.
- `DATA_REPAIR`, `REPLAY_REPAIR`, `METRICS_REPAIR`: fix evidence plumbing.
- `NO_TRADE_RESEARCH` / `NEGATIVE_REJECT`: keep as context or reject.

The arbiter does not relax gates. It cannot trade, cannot promote, and cannot
turn proxy or sparse evidence into a paper lane.

## Port Families

The lab groups the 95 titles into VNEDGE-owned build families:

- `bitcoin_crypto_strategy_pack_v1`
- `range_volatility_breakout_reversion_v1`
- `pullback_reversion_pack_v1`
- `indicator_pack_mtf_v1`
- `trend_momentum_pack_v1`
- `price_action_structure_pack_v1`
- `crypto_session_calendar_miner_v1`
- `crypto_relative_strength_rotation_v1`
- `short_tail_risk_pack_v1`
- `ensemble_blend_lab_v1`
- `swing_template_crypto_rebuild_v1`

## Fast Track

Chunk A should be built first: crypto-native, breakout/reversion, and pullback
families. Those map best to the bot's current scanner stack and to the active
TP1/breakeven/trailing exit work.

Chunk B converts classic indicators into edge-model features rather than hard
entry gates.

Chunk C remaps equity overnight/month-end/weekday ideas to crypto-native clocks:
UTC day boundary, Asia/London/NY sessions, funding windows, and weekend
liquidity changes.

Chunk D rebuilds rotation ideas as cross-pair crypto relative strength.

Chunk Q is quarantine: bond, DAX, ETF-sector, placeholder, and unreleased titles
stay research-only until a clean crypto thesis exists.

## Promotion Contract

A port is not tradable from this lab until it has:

- causal VNEDGE implementation,
- 1m/5m/15m/1h/4h replay where data exists,
- maker/taker/slippage fee-wall accounting,
- active TP1/TP2/TP3 and breakeven/trailing comparison,
- expected net edge greater than 25 bps,
- PF greater than 1.5,
- at least 20 historical trades,
- untouched-window judgment.

This lab is a factory intake, not an execution permission surface.
