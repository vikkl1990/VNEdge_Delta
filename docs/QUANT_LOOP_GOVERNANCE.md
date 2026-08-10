# Quant Loop Governance

`quant_loop_governance_v1` is the control-plane layer for VNEDGE AI Quant OS
research loops.

It is adapted from loop-engineering patterns: explicit loop state,
machine-readable gates, run logs, collision locks, budget checks, and a
checker-style readiness score. It does not search for alpha by itself. It keeps
the alpha-search loops honest and observable.

## Inputs

- `governance/loop_gates.yaml`
- `research/quant_loop_state.json`
- `research/live_research/alpha_arena_lite_latest.json`
- `research/live_research/scanner_backtest_uplift_latest.json`
- `research/live_research/scanner_tournament_progress.json`
- `logs/agent_gateway/quant_os/snapshot.json`
- `research/live_research/quant_loop_run_log.jsonl`

## Outputs

- `research/live_research/quant_loop_governance_latest.json`
- `research/live_research/quant_loop_governance_feed.jsonl`
- appended records in `research/live_research/quant_loop_run_log.jsonl`

Every output is research-only:

- `can_trade=false`
- `can_promote=false`
- `live_orders_enabled=false`

## What It Checks

- Whether research artifacts are present and fresh.
- Whether Alpha Arena candidates collide on the same
  `strategy_id/exchange/symbol/timeframe/data_window` lock.
- Whether loop budgets have been exceeded today.
- Whether the materialized gate configuration names the active versioned
  `PromotionPolicy` and preserves its governance floors: 25 bps net edge,
  PF 1.5, and 20 trades under policy `v1.2.0`.
- Whether verifier-before-paper, untouched-window, and burn-registry policy are
  present in the machine-readable gates.

## Readiness Levels

- `L0_BLOCKED`: governance artifacts or gates are broken.
- `L1_BOOTSTRAPPING`: loop wiring exists, but important evidence is missing.
- `L2_LOOP_HEALTHY_WAITING_EVIDENCE`: loops are usable but still waiting on
  samples or proof.
- `L3_GOVERNED_RESEARCH_READY`: research loops are coordinated and fresh.

None of these levels means paper, shadow, or live readiness. Paper promotion
still requires the normal VNEDGE ladder: causal port, fee-aware replay,
untouched-window judgment, verifier review, and explicit operator approval.

## Run

```bash
python -m vnedge.research.quant_loop_governance \
  --gates governance/loop_gates.yaml \
  --state research/quant_loop_state.json \
  --alpha-arena research/live_research/alpha_arena_lite_latest.json \
  --scanner-uplift research/live_research/scanner_backtest_uplift_latest.json \
  --scanner-progress research/live_research/scanner_tournament_progress.json \
  --gateway-snapshot logs/agent_gateway/quant_os/snapshot.json \
  --out research/live_research/quant_loop_governance_latest.json \
  --feed research/live_research/quant_loop_governance_feed.jsonl \
  --run-log research/live_research/quant_loop_run_log.jsonl
```

Docker Compose refreshes this continuously as `quant-loop-governance`.

## Agentic Supervisor

`agentic_research_os_v2` reads the Quant Loop Governance artifact together with
Vibe Intelligence, Alpha Arena, the Quant OS Agent Gateway snapshot, and Paper
Lane Performance. It publishes the operator queue for agent work:

- verify untouched-window candidates
- repair stale or failed tasks
- decay negative paper lanes
- retire repeatedly decayed hypotheses

It does not change this governance verdict and it cannot trade or promote.

## Dashboard

The Pine Research Lab reads:

- `GET /pine-research/quant-loop-governance`

The panel shows readiness score, loop health, collisions, budget alerts, and the
top loop actions.
