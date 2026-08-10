# Agentic Research OS v2

`agentic_research_os_v2` is the supervisor layer for VNEDGE AI Quant OS.

It reads the existing research agents and turns their outputs into one
operator queue: what to keep researching, what needs verifier proof, what is
stale, what should be repaired, and what should be retired.

It does not run backtests, mutate strategy code, promote lanes, or place
orders.

## Why This Exists

VNEDGE now has many research components:

- Vibe Intelligence tracks hypothesis lifecycle.
- Alpha Arena turns source-backed ideas into proof tasks.
- Quant OS Agent Gateway stores durable tasks, events, and artifacts.
- Quant Loop Governance checks loop freshness, collisions, and budgets.
- Paper Lane Performance reports whether promoted observation lanes are
  healthy or decaying.

Before this layer, the operator still had to mentally join those surfaces.
Agentic Research OS v2 is the joiner. It answers:

- Which agent track is healthy?
- Which stale task needs reclaiming?
- Which candidate needs untouched-window verification?
- Which paper lane should be decayed or repaired?
- Which repeated negative hypothesis should stop consuming cycles?

## Inputs

Default inputs:

- `research/live_research/vibe_intelligence_latest.json`
- `research/live_research/alpha_arena_lite_latest.json`
- `logs/agent_gateway/quant_os/snapshot.json`
- `research/live_research/quant_loop_governance_latest.json`
- `research/live_research/paper_lane_performance_latest.json`

Missing inputs are allowed. They become `MISSING` source status rows and lower
the agent scorecard health instead of crashing the publisher.

## Outputs

Default outputs:

- `research/live_research/agentic_research_os_latest.json`
- `research/live_research/agentic_research_os_feed.jsonl`

The latest payload contains:

- `summary`: counts, buckets, critical actions, minimum agent health.
- `agent_scorecards`: health of each research agent lane.
- `operator_queue`: ranked keep, verify, repair, expand, decay, and retire
  actions.
- `source_status`: freshness and presence of each upstream artifact.
- `operator_answer`: short plain-English status.

Every payload is research-only:

- `can_trade=false`
- `can_promote=false`
- `live_orders_enabled=false`

## Run

One-shot:

```bash
python -m vnedge.research.agentic_research_os --json
```

Continuous publisher:

```bash
python -m vnedge.research.agentic_research_os \
  --interval-seconds 900 \
  --vibe research/live_research/vibe_intelligence_latest.json \
  --alpha-arena research/live_research/alpha_arena_lite_latest.json \
  --gateway-snapshot logs/agent_gateway/quant_os/snapshot.json \
  --quant-loop research/live_research/quant_loop_governance_latest.json \
  --paper-performance research/live_research/paper_lane_performance_latest.json \
  --out research/live_research/agentic_research_os_latest.json \
  --feed research/live_research/agentic_research_os_feed.jsonl
```

Docker Compose runs it as `agentic-research-os`.

## Dashboard

The Operator Cockpit reads:

- `GET /agentic-research-os`

The Research view renders:

- action count
- critical action count
- verifier queue size
- retire queue size
- agent health rows
- top operator queue rows

This panel is an operating compass, not a launch button. It should make the
next research step obvious while preserving VNEDGE's promotion ladder.

## Design Boundary

Agentic Research OS v2 may recommend:

- `REQUEST_UNTOUCHED_VERIFIER`
- `EXPAND_SAMPLE_ON_NEXT_UNTOUCHED_WINDOW`
- `RUN_EXECUTION_SALVAGE_BEFORE_MORE_ENTRIES`
- `DECAY_OR_REPAIR_PAPER_LANE`
- `RETIRE_HYPOTHESIS`
- `RECLAIM_OR_FAIL_STALE_TASK`

It may not:

- create a paper lane
- change live or paper profiles
- weaken promotion gates
- submit orders
- bypass `PreTradeRiskGateway.evaluate()`

That separation is the core architecture point: agents can sharpen the research
queue, but execution still stays under the human-approved VNEDGE ladder.
