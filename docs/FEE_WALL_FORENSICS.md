# Fee-Wall Forensics

VNEDGE scalper research must separate three different failures that used to
look identical in the reports:

1. Price never moved enough to pay route cost.
2. Price moved enough, but the exit gave the move back.
3. The command did not see the candle dataset, so no strategy verdict exists.

The execution edge labeler and router now publish explicit trade-geometry
fields for every labeled event:

- `hold_bars`: bars held from next-open entry to stop, target, or horizon.
- `time_to_mfe_bars`: bars needed to reach maximum favorable excursion.
- `mfe_after_cost_bps`: best favorable move minus maker/taker route cost.
- `capture_ratio`: realized gross bps divided by MFE bps.
- `fee_wall_break_rate_pct`: percentage of routed events whose MFE cleared
  fees before exit.
- `exit_diagnosis`: `MOVE_NEVER_CLEARED_COST`,
  `GAVE_BACK_FEE_WALL_MOVE`, `STOP_BEFORE_TARGET`,
  `HORIZON_EXIT_FAILED_CAPTURE`, or `CAPTURED_AFTER_COST`.

Use this to tune scalpers as chart systems, not only as pass/fail scanners:

```bash
python -m vnedge.research.execution_edge_router \
  --exchange binanceusdm \
  --symbol ETHUSDT \
  --timeframe 15m \
  --lookback-days 30 \
  --horizon-bars 8 \
  --min-edge-bps 8 \
  --min-profit-factor 1.15
```

Interpretation:

- `MOVE_NEVER_CLEARED_COST`: entry thesis is too weak for the venue/route.
  More exit tuning will not rescue it.
- `GAVE_BACK_FEE_WALL_MOVE`: the signal found movement, but target/trail/horizon
  is wrong. This is the best candidate for sniper-target work.
- `STOP_BEFORE_TARGET`: stop geometry is too tight or entry is late.
- `HORIZON_EXIT_FAILED_CAPTURE`: the holding window missed the move timing.
- `CAPTURED_AFTER_COST`: this is what a promoted scanner must concentrate.

If the CLI reports a missing candle lane, treat that as a data/config problem,
not a market verdict. Fix the downloader, symbol alias, or container volume
mount before judging edge.

## Batch Scanner Sweep

The batch runner turns the single-lane router into an operator artifact:

```bash
python -m vnedge.research.fee_wall_forensics \
  --timeframes 5m,15m,1h,4h \
  --lookback-days 30 \
  --min-samples 10 \
  --min-edge-bps 8 \
  --min-profit-factor 1.15
```

Published files:

- `research/live_research/fee_wall_forensics_latest.json`: every compact
  venue/symbol/timeframe/strategy report, top rows, strict fee-wall candidates,
  sparse-positive candidates, and exit-salvage candidates.
- `research/live_research/fee_wall_forensics_progress.json`: current unit,
  progress percentage, row count, and route count for UI visibility.
- `research/live_research/fee_wall_forensics_routes_latest.jsonl`: every
  routed or skipped opportunity row for later edge-model training.
- `research/live_research/fee_wall_forensics_feed.jsonl`: compact historical
  feed for the council/workbench.

Use `sample_expansion_candidates` for the exact situation we saw on the VM:
large net bps and `CAPTURED_AFTER_COST`, but fewer than the required samples.
The next research step is not promotion; it is expanding the sample honestly by
longer lookback, lower-timeframe trigger replay, or widening the symbol/venue
universe.

Use `strict_fee_wall_candidates` when the row already clears the configured
sample, net-bps, and PF floors inside this research run. That still is not a
promotion. The correct next action is a pre-registered untouched-window
judgment, not immediate paper/live deployment.

## Live-Data Paper Probes

After explicit human approval, the multi-lane runtime can turn strict fee-wall
rows into isolated **paper probes**:

```bash
MULTI_LANE_FEE_WALL_PAPER_PROBES=1 \
python -m vnedge.runtime.multi_lane_shadow
```

The preferred durable source is
`research/live_research/fee_wall_paper_probes.json` with a `paper_probes` list.
If that file is absent, the runner can bootstrap from a fresh
`fee_wall_forensics_latest.json`. This prevents an approved multi-day paper
probe from disappearing on restart just because the original research artifact
aged out.

These lanes simulate fills on live public market data through the same gateway,
journal, order manager, and `PaperBroker` path as every other paper lane. They
are not live-capital lanes, do not mount a live adapter, and write separate
`fee_wall_*_paper_probe` account/journal/fill ledgers so they cannot collide
with governed paper trials.

Default probe guards:

- latest artifact path:
  `research/live_research/fee_wall_forensics_latest.json`
- durable manifest path:
  `research/live_research/fee_wall_paper_probes.json`
- max artifact age: 72 hours
- routed opportunities: >= 10
- average selected net edge: >= 8 bps
- profit factor: >= 1.15
- verdict: `MAKER_EDGE` or `MIXED_ROUTE_EDGE`
- recommended action:
  `PRE_REGISTER_UNTOUCHED_JUDGMENT_WINDOW`

A paper probe is for live-data sample expansion and execution-behaviour
evidence. It still cannot promote to shadow/live without the normal untouched
judgment and ladder evidence.

Use `exit_salvage_candidates` when `avg_mfe_after_cost_bps` is positive but
realized net is not. That means the entry found enough movement to beat fees,
but the stop/target/trail/horizon failed to capture it. Those are the best
rows for sniper-target and trailing-stop work.
