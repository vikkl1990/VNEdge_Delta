# Alpha Council

The Alpha Council is VNEDGE's agent-debate layer for strategy discovery. It is
inspired by agent-native trading platforms where many agents publish and
challenge ideas, but it is deliberately research-only.

## What It Does

Every cycle, `python -m vnedge.research.alpha_council` reads the existing
research artifacts in `research/live_research/`:

- `latest.json` rolling walk-forward research
- `event_leadlag_latest.json`
- `l2_scout_latest.json`
- `l2_latest.json`
- `daily_scalper_latest.json`
- `alpha_distillation_latest.json`
- `bitcoin_regime_latest.json`
- `candidate_replay_latest.json`
- `execution_condition_latest.json`
- `filtered_replay_latest.json`

It extracts candidates, then runs five deterministic agents:

- `edge_advocate`: argues for the edge evidence
- `skeptic`: attacks sample size, overfit, weak gates, and unresolved rejects
- `execution_specialist`: checks maker/taker feasibility and fee wall
- `risk_governor`: applies promotion-ladder vetoes
- `research_director`: converts the debate into the next proof step

The output is written to:

- `research/live_research/alpha_council_latest.json`
- `research/live_research/alpha_council_feed.jsonl`

The Alpha Workbench consumes this output and checkpoints each next-action into
durable replay, judgment, or recording tasks under
`research/live_research/alpha_workbench/`. The council debates; the workbench
remembers the work.

The council also applies source quotas so one noisy feed cannot crowd out the
rest of the signal funnel. Event lead-lag, candle walk-forward, daily scalper,
alpha distillation, L2 scouts, Bitcoin network-regime context, and
artifact-health rows each get bounded representation in the debate queue.

Bitcoin regime rows are context-only. A calm/healthy BTC network is ignored.
Stressed or unhealthy node/mempool state is routed into proof work:

- healthy but stressed fee market -> `SPLIT_REPLAY_BY_BTC_REGIME`
- missing, stale, or unsynced source -> `REFRESH_BITCOIN_NODE_HEALTH`

This lets replay answer whether edges are conditional on Bitcoin fee-market
stress without allowing mempool telemetry to create an order.

Positive-after-fees lanes that still fail gates are not treated as generic
rejects. They are routed into concrete repair work:

- payoff / PF failures -> `REPAIR_EXIT_PAYOFF`
- zero-trade-window / sparse-window failures -> `CHECK_ZERO_WINDOW_STABILITY`
- close rejects without a clear class -> `DIAGNOSE_CLOSE_REJECT`

Missing or stale artifacts become `REFRESH_STALE_ARTIFACT` tasks, which keeps
the bot from confusing an empty signal funnel with an unhealthy research
producer.

Microstructure candidates are not allowed to jump from scanner output to
shadow. They first require `candidate_replay_latest.json`, which proves the
event through passive quote placement, conservative maker fill, taker exit, and
fees. If that replay fails, `execution_condition_latest.json` can turn the
failure into a concrete next experiment:

- no replay yet -> `RUN_CONSERVATIVE_L2_REPLAY`
- replay passed -> `QUEUE_SHADOW_TRIAL_AFTER_REPLAY`
- replay failed without condition report -> `MINE_PRE_EVENT_EXECUTION_CONDITIONS`
- replay failed with a filter proposal -> `RUN_FILTERED_REPLAY_FROM_EXECUTION_CONDITIONS`
- filtered replay passed on a fresh slice -> `QUEUE_SHADOW_TRIAL_AFTER_REPLAY`

## What It Never Does

- No orders
- No paper/shadow/live promotion
- No parameter mutation
- No model hot swap
- No bypass of `PreTradeRiskGateway`

The council may rank an idea as high-priority, but the payload always has
`can_trade=false` and `can_promote=false`.

## Why This Shape

For scalping, the failure mode is not a lack of ideas; it is false confidence.
The council therefore separates roles:

- the advocate keeps us from ignoring weak-but-real pulses
- the skeptic keeps us from overfitting
- execution keeps the fee wall and fill assumptions explicit
- risk keeps the promotion ladder intact

The next phase is to let sandboxed AI proposal agents generate candidate
families, but those proposals should feed this council as evidence rows and
still require replay, untouched judgment, shadow, and paper validation.
