# Filtered Replay Executor

`vnedge.research.filtered_replay_executor` is the proof step after the
execution-condition miner.

It reads:

- `research/live_research/event_leadlag_latest.json`
- `research/live_research/orderflow_footprint_latest.json`
- `research/live_research/candidate_replay_latest.json`
- `research/live_research/execution_condition_latest.json`
- recorded public tick/book data under `data/ticks/...`

It writes:

- `research/live_research/filtered_replay_latest.json`
- `research/live_research/filtered_replay_feed.jsonl`

## Contract

Filtered replay does not trade, promote, or loosen gates. It:

1. selects only candidates whose execution-condition report requests
   `RUN_FILTERED_REPLAY_FROM_EXECUTION_CONDITIONS`
2. excludes prior candidate-replay days by default
3. applies only causal/pre-entry filters such as quote-window spread and
   pre-trigger signed tape
4. reruns the same conservative maker-entry/taker-exit replay engine

A filtered replay pass can move the candidate to
`QUEUE_SHADOW_TRIAL_AFTER_REPLAY`, but it still requires a governed shadow
manifest and human approval before any paper/live lane.

Run once:

```bash
python -m vnedge.research.filtered_replay_executor --data-root data
```

Run as a service:

```bash
python -m vnedge.research.filtered_replay_executor \
  --data-root data \
  --interval-seconds 1800
```

By default the executor refuses to reuse prior replay days. The
`--allow-seen-window` flag exists for diagnostics only and must not be used for
promotion evidence.
