# VNEDGE Vibe Intelligence

`vnedge.research.vibe_intelligence` is the VNEDGE-safe adaptation of the
agentic workflow ideas in HKUDS/Vibe-Trading: memory-backed research,
multi-agent review, shadow diagnostics, and strategy lifecycle tracking.

It does **not** copy Vibe-Trading or grant agents trading authority. It joins
the existing VNEDGE alpha council and alpha workbench into a persistent
hypothesis board that remembers what has been tried, what is still active, what
is being monitored, and what should be decayed or disabled.

## Why this exists

The alpha council debates candidates each cycle. The alpha workbench turns those
debates into proof tasks. Before this layer, VNEDGE still lacked a durable
operator-facing memory of hypothesis health. That meant the same failed idea
could keep returning as fresh-looking work.

Vibe Intelligence fixes that by creating a lifecycle card per hypothesis:

- `INCUBATING`: useful context or infrastructure work, but no falsifiable edge
  proof yet.
- `ACTIVE`: a hypothesis has a concrete next proof step, such as conservative
  replay, untouched judgment, or fresh tick collection.
- `MONITORING`: a replay-passed candidate is ready for governed shadow or paper
  observation.
- `DECAYED`: replay, execution, fee-wall, or shadow/paper evidence says the
  idea is currently unhealthy.
- `DISABLED`: repeated decay makes the idea ineligible for more automatic
  attention until a human explicitly reframes it.

## Inputs

The module reads, or is passed directly:

- `research/live_research/alpha_council_latest.json`
- `research/live_research/alpha_workbench_latest.json` or a fresh workbench
  build from the same council payload

Each council debate is joined to the workbench task for the same candidate. The
resulting card carries the candidate, proof step, blocked-by list, score stack,
and lifecycle state.

## Outputs

Default latest artifact:

```bash
python -m vnedge.research.vibe_intelligence --once
```

Writes:

- `research/live_research/vibe_intelligence_latest.json`
- `research/live_research/vibe_intelligence_feed.jsonl`
- `research/live_research/vibe_intelligence/manifest.json`
- `research/live_research/vibe_intelligence/chunks/*.json`

The manifest is the long memory. It tracks `times_seen`,
`decay_observations`, lifecycle state, score, and the content-addressed chunk
for each hypothesis.

## Safety boundary

This layer is research-only:

- `can_trade=false`
- `can_promote=false`
- `live_orders_enabled=false`
- no order intents
- no strategy parameter mutation
- no auto-promotion

The only way any hypothesis can move toward trading remains the VNEDGE ladder:
replay, untouched judgment, human approval, shadow, paper, and then the live
checklist through `PreTradeRiskGateway.evaluate()`.

## Operator reading

Use the summary to answer:

- What is truly active right now?
- Which candidates are only monitoring candidates?
- Which repeated ideas are decaying and should stop consuming cycles?
- What is the next falsifiable proof step for each candidate?

This is not an edge generator by itself. It is the memory and lifecycle control
plane that keeps the edge factory from looping blindly.
