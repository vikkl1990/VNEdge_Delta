# Alpha Arena Lite

`alpha_arena_lite_v1` is the research-only bridge between scanner uplift rows,
strict fee-wall evidence, and the Quant OS Agent Gateway.

It exists because the scanner uplift panel can surface rows like:

- `SPARSE_POSITIVE`
- high net bps
- PF `999.00`
- fewer than 20 trades

That is useful, but it is not paper-ready. Alpha Arena Lite converts scanner
uplift experiments and canonical strict fee-wall candidates into durable tasks
plus hash-backed scorecards so the next proof step is explicit and repeatable.

## Inputs

- `research/live_research/scanner_backtest_uplift_latest.json`
- `research/live_research/scanner_tournament_latest.json`
- `research/live_research/fee_wall_forensics_latest.json`

The tournament payload is optional, but when present it enriches scorecards with
MFE/MAE, routed count, and opportunity count.

Strict candidates are de-duplicated by strategy, venue, symbol, and timeframe.
If scanner uplift and fee-wall forensics both describe the same lane, Arena
emits one `PRE_REGISTER_UNTOUCHED_JUDGMENT` scorecard with both evidence
sources attached.

## Output

- `research/live_research/alpha_arena_lite_latest.json`
- `research/live_research/alpha_arena_lite_feed.jsonl`
- `logs/agent_gateway/quant_os/tasks.jsonl`
- `logs/agent_gateway/quant_os/artifacts.jsonl`

The output is always:

- `can_trade=false`
- `can_promote=false`
- `live_orders_enabled=false`

## Verdicts

- `EXPAND_UNTOUCHED_SAMPLE`: positive but under the minimum trade-count gate.
- `EXECUTION_SALVAGE_REQUIRED`: close to the fee wall; test route and capture
  improvements before more entries.
- `SELECTIVITY_OR_EXIT_UPLIFT`: positive structure, but not proof quality.
- `FEATURE_BANK_OR_REJECT`: recycle as model input or reject as standalone.
- `PRE_REGISTER_UNTOUCHED_JUDGMENT`: enough evidence for a one-shot judgment
  request, still not paper promotion.

## Run

```bash
python -m vnedge.research.alpha_arena_lite \
  --uplift research/live_research/scanner_backtest_uplift_latest.json \
  --scanner research/live_research/scanner_tournament_latest.json \
  --fee-wall research/live_research/fee_wall_forensics_latest.json \
  --out research/live_research/alpha_arena_lite_latest.json \
  --feed research/live_research/alpha_arena_lite_feed.jsonl
```

Docker Compose runs this continuously as `alpha-arena-lite`.

`quant_loop_governance_v1` watches this output for stale artifacts, duplicate
candidate locks, and budget pressure before the operator trusts the next sample
expansion step. See [QUANT_LOOP_GOVERNANCE.md](QUANT_LOOP_GOVERNANCE.md).

## Operator Rule

Alpha Arena Lite can prove that a row deserves more research. It cannot approve
paper, shadow, or live trading. Sparse positives must be expanded on the next
untouched window with frozen parameters. Strict candidates must be judged on a
pre-registered untouched window before any paper manifest is discussed.
