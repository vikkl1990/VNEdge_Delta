# Execution Condition Miner

`vnedge.research.execution_condition_miner` is a research-only post-mortem
for conservative replay failures.

It reads:

- `research/live_research/candidate_replay_latest.json`
- recorded public tick/book data under `data/ticks/...`

It writes:

- `research/live_research/execution_condition_latest.json`
- `research/live_research/execution_condition_feed.jsonl`

The miner does not trade, promote, or relax gates. It classifies why a replay
row failed:

- `SPREAD_TOO_WIDE`
- `STALE_OR_MISSING_BOOK`
- `NO_TRADE_THROUGH`
- `TOUCH_ONLY_QUEUE_RISK`
- `LOW_FILL_RATE`
- `ADVERSE_SELECTION`
- `WRONG_WAY_DRIFT`
- `FEE_WALL_FAIL`
- `DATA_GAP`

Filter proposals are only next experiments. A row that fails replay and then
gets a filter proposal must run `RUN_FILTERED_REPLAY_FROM_EXECUTION_CONDITIONS`
on a fresh or explicitly governed slice before it can enter any shadow-trial
discussion.

Run once:

```bash
python -m vnedge.research.execution_condition_miner --data-root data
```

Run as a service:

```bash
python -m vnedge.research.execution_condition_miner \
  --data-root data \
  --interval-seconds 1800
```

Docker Compose runs it after `candidate-replay-executor` and before the Alpha
Council consumes the output.
