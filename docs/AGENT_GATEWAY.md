# VNEDGE Agent Gateway

The Agent Gateway is the first slice of the **VNEDGE AI Quant OS** surface:
an AI-facing, scoped API under `/api/agent/v1` and `/api/agent/v2` that lets
research agents read the bot's state, request proof work, append task events,
and publish evidence artifacts without touching exchange keys, strategy source,
order routing, or live controls.

It is deliberately separate from the human dashboard API:

- dashboard routes use `DASHBOARD_TOKEN` / `DASHBOARD_USERS`;
- agent routes use `AGENT_GATEWAY_TOKENS_JSON`;
- dashboard tokens do not authenticate agents;
- agent tokens do not authenticate the dashboard.

## Safety Boundaries

This first phase is research-only.

- No order route is exposed.
- No live trading route is exposed.
- Backtest requests are recorded as `PENDING_RESEARCH_ONLY` jobs.
- Quant OS v2 tasks are recorded as `*_RESEARCH_ONLY` durable tasks.
- Every job has `can_trade=false`, `can_promote=false`, and
  `live_orders_enabled=false`.
- Every task, event, and artifact has `can_trade=false`, `can_promote=false`,
  and `live_orders_enabled=false`.
- Every authenticated agent route appends a hash-chained JSONL audit record.
- Tokens are paper-only by default.

The existing VNEDGE ladder remains the only route toward capital:

```text
research -> untouched judgment -> human approval -> paper -> shadow -> live_small -> live_full
```

## Configure Tokens

Set `AGENT_GATEWAY_TOKENS_JSON` on the dashboard/multi-lane service. Empty
means the gateway is not mounted.

Prefer `token_sha256` in production:

```bash
python3 - <<'PY'
import hashlib, secrets
token = "agd_" + secrets.token_urlsafe(32)
print("token:", token)
print("sha256:", hashlib.sha256(token.encode()).hexdigest())
PY
```

Example `.env` value:

```json
[
  {
    "name": "alpha-council",
    "token_sha256": "REPLACE_WITH_64_HEX_SHA256",
    "scopes": ["R", "B"],
    "paper_only": true,
    "rate_limit_per_min": 60,
    "markets": ["binanceusdm:BTC/USDT:USDT", "bybit:BTC/USDT:USDT"],
    "lanes": ["*"]
  }
]
```

For local development only, `"token": "raw-secret"` is accepted and hashed
immediately at startup. The raw token is not retained on the token object.

## Scopes

| Scope | Meaning |
|---|---|
| `R` | Read gateway-safe bot state and research artifacts. |
| `B` | Submit research-only backtest job requests. |
| `W_RESEARCH` | Create durable Quant OS research tasks, append progress events, and publish evidence artifacts. |
| `T_PAPER` | Reserved for future paper-only agent actions. No route uses it yet. |

There is intentionally no live-trading scope in this phase.

## Routes

All routes except `/health` require `Authorization: Bearer <agent-token>`.

| Route | Scope | Purpose |
|---|---:|---|
| `GET /api/agent/v1/health` | none | Gateway status and available route list. |
| `GET /api/agent/v1/whoami` | token | Token identity, scopes, allowlists, paper-only flag. |
| `GET /api/agent/v1/state` | `R` | Latest coalesced dashboard snapshot. |
| `GET /api/agent/v1/lanes` | `R` | Agent-friendly lane summary distilled from the snapshot. |
| `GET /api/agent/v1/research/latest` | `R` | Latest continuous-research verdict payload. |
| `GET /api/agent/v1/alpha-council` | `R` | Latest deterministic council debate output. |
| `GET /api/agent/v1/alpha-workbench` | `R` | Latest proof-task backlog. |
| `GET /api/agent/v1/vibe-intelligence` | `R` | Latest hypothesis lifecycle memory. |
| `GET /api/agent/v1/lane-readiness` | `R` | Latest paper/shadow promotion readiness report. |
| `GET /api/agent/v1/realtime-scanner` | `R` | Latest live-observation scanner report. |
| `GET /api/agent/v1/jobs` | `R` | List agent research jobs. |
| `GET /api/agent/v1/jobs/{job_id}` | `R` | Read one agent research job. |
| `POST /api/agent/v1/backtests` | `B` | Record a strict, research-only backtest request. |
| `GET /api/agent/v2/health` | none | Quant OS task/event/artifact ledger status. |
| `GET /api/agent/v2/tasks` | `R` | Read durable Quant OS task snapshot. |
| `POST /api/agent/v2/tasks` | `W_RESEARCH` | Create a research-only task. |
| `GET /api/agent/v2/events` | `R` | Read recent Quant OS event stream payloads. |
| `POST /api/agent/v2/tasks/{task_id}/events` | `W_RESEARCH` | Append task progress / heartbeat / failure context. |
| `GET /api/agent/v2/artifacts` | `R` | Read artifact registry metadata. |
| `POST /api/agent/v2/tasks/{task_id}/artifacts` | `W_RESEARCH` | Publish hash-backed research evidence. |

## Backtest Request Example

```bash
curl -sS \
  -H "Authorization: Bearer $AGENT_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "strategy_id": "sats_5m_scalper_v1",
    "exchange": "delta_india",
    "symbol": "ETH/USDT:USDT",
    "timeframe": "5m",
    "hypothesis_id": "sats-eth-delta-bbp-stealthtrail",
    "strict_mode": true,
    "live_orders_enabled": false,
    "parameters": {"note": "agent-requested exploratory replay"}
  }' \
  http://127.0.0.1:8080/api/agent/v1/backtests
```

The response is a job ledger record, not a promotion:

```json
{
  "job_id": "agj_...",
  "kind": "backtest_request",
  "status": "PENDING_RESEARCH_ONLY",
  "can_trade": false,
  "can_promote": false,
  "live_orders_enabled": false
}
```

## Job Runner

`python -m vnedge.agent_gateway.job_runner` consumes pending backtest jobs and
writes terminal research-only evidence. It runs from recorded local data only:

- registered strategies are backtested against Parquet candles in
  `data/normalized/...`;
- AI-authored `ai_*` strategies are routed through the sandboxed candidate
  research surface;
- microstructure replay jobs route through the conservative candidate replay
  executor and consume only standard mined artifacts plus recorded tick/L2
  data;
- missing data, unknown strategies, non-strict jobs, or live-enabled requests
  become `BLOCKED_RESEARCH_ONLY` with a concrete reason;
- unexpected execution errors become `FAILED_RESEARCH_ONLY`;
- successful jobs become `DONE_RESEARCH_ONLY`.

No terminal state authorizes trading:

```json
{
  "status": "DONE_RESEARCH_ONLY",
  "result": {
    "metrics": {"num_trades": 12, "net_profit_usd": -3.42},
    "promotion_verdict": "NOT_EVALUATED_AGENT_JOB",
    "can_trade": false,
    "can_promote": false,
    "live_orders_enabled": false
  }
}
```

Run once:

```bash
python -m vnedge.agent_gateway.job_runner --once --json
```

The Docker Compose service `agent-job-runner` runs continuously and shares
only:

- `./logs` read/write for the job ledger;
- `./data` read-only for market data;
- `./research/live_research` read/write for artifacts under
  `agent_jobs/<job_id>.json`.

Tuning knobs:

| Env | Default | Meaning |
|---|---:|---|
| `AGENT_GATEWAY_JOBS_DIR` | `logs/agent_gateway/jobs` | Queue directory shared with the dashboard service. |
| `AGENT_JOB_RUNNER_INTERVAL_SECONDS` | `60` | Poll cadence. |
| `AGENT_JOB_RUNNER_MAX_PER_CYCLE` | `1` | Bounded jobs per poll. |
| `AGENT_JOB_RUNNER_DATA_ROOT` | `data` | Recorded data root. |
| `AGENT_JOB_RUNNER_ARTIFACT_DIR` | `research/live_research/agent_jobs` | JSON evidence output. |
| `AGENT_JOB_RUNNER_SEED_DEFAULTS` | `1` in Compose | Idempotently enqueue the Quant OS starter research jobs on worker startup. |

The human dashboard also exposes a token-gated `/agent-jobs` endpoint and the
Quant OS Job Ledger panel. This is deliberately separate from
`/api/agent/v1/jobs`: operators can inspect the queue with the dashboard token
even when the Agent Gateway HTTP surface is not mounted because no agent tokens
are configured.

## Quant OS Agent Gateway v2

Agent Gateway v2 is the durable task bus for the next Alpha Arena Lite phase.
It is not an executor. It gives agents a reliable way to say:

- what research task exists;
- what happened while the task ran;
- what artifact proves the result.

Artifacts published with inline `content` are written under
`<QUANT_OS_AGENT_GATEWAY_DIR>/artifacts/<task_id>/` and registered with byte
size and SHA-256. Artifacts published by `path` must already live under
`<QUANT_OS_AGENT_GATEWAY_DIR>`; they are hashed in place and registered by
metadata only. The dashboard never serves artifact contents directly.

Create a task:

```bash
curl -sS \
  -H "Authorization: Bearer $AGENT_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "kind": "alpha_arena.experiment",
    "objective": "rank ETH Delta FVG and trail-exit ports before replay",
    "priority": 90,
    "target": {"exchange": "delta_india", "symbol": "ETH/USD:USD", "timeframe": "5m"},
    "payload": {"families": ["fvg_liquidity_breakout_v1", "trail_exit_lab_v1"]},
    "live_orders_enabled": false
  }' \
  http://127.0.0.1:8080/api/agent/v2/tasks
```

Append progress:

```bash
curl -sS \
  -H "Authorization: Bearer $AGENT_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"event_type":"BACKTEST_STARTED","message":"started causal replay"}' \
  http://127.0.0.1:8080/api/agent/v2/tasks/$TASK_ID/events
```

Publish a scorecard artifact:

```bash
curl -sS \
  -H "Authorization: Bearer $AGENT_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "artifact_type": "arena_scorecard",
    "summary": "first OOS route scorecard",
    "content": {"pf": 1.21, "net_bps": -4.5, "strict": true}
  }' \
  http://127.0.0.1:8080/api/agent/v2/tasks/$TASK_ID/artifacts
```

Operator dashboard reads:

- `GET /quant-os/agent-gateway`
- `GET /quant-os/agent-gateway/events`
- `GET /pine-research/alpha-arena-lite`
- `GET /pine-research/quant-loop-governance`

The events route also returns finite Server-Sent Event frames when requested
with `Accept: text/event-stream`, useful for thin operator consoles.

`alpha_arena_lite_v1` now uses this gateway to attach durable scanner
sample-expansion scorecards. See [ALPHA_ARENA_LITE.md](ALPHA_ARENA_LITE.md).
`quant_loop_governance_v1` supervises loop state, collision locks, and run
budgets. See [QUANT_LOOP_GOVERNANCE.md](QUANT_LOOP_GOVERNANCE.md).

Seed the starter jobs manually:

```bash
python -m vnedge.agent_gateway.seed_jobs --json
```

The seeded jobs are:

- `sats_5m_scalper_v1` on Delta India `ETH/USD:USD` 5m candles;
- `candidate_replay_executor_v1` for conservative L2/order-flow replay;
- `ai_example_ma_cross` for the sandbox AI candidate pipeline.

They are ordinary research-only jobs: `strict_mode=true`,
`live_orders_enabled=false`, `can_trade=false`, `can_promote=false`.

## Conservative Replay Job Example

Agents can request the replay adapter without registering a trading strategy:

```bash
curl -sS \
  -H "Authorization: Bearer $AGENT_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "strategy_id": "candidate_replay_executor_v1",
    "exchange": "delta_india",
    "symbol": "ETH/USDT:USDT",
    "timeframe": "1m",
    "hypothesis_id": "orderflow-delta-eth-replay",
    "strict_mode": true,
    "live_orders_enabled": false,
    "parameters": {
      "max_event_leadlag": 5,
      "max_orderflow": 25,
      "min_replay_fills": 5,
      "queue_aware": true
    }
  }' \
  http://127.0.0.1:8080/api/agent/v1/backtests
```

Equivalent adapter form:

```json
{
  "strategy_id": "agent_named_microstructure_hypothesis",
  "exchange": "binanceusdm",
  "symbol": "SOL/USDT:USDT",
  "timeframe": "1m",
  "strict_mode": true,
  "live_orders_enabled": false,
  "parameters": {"adapter": "candidate_replay"}
}
```

The runner reads:

- `research/live_research/event_leadlag_latest.json`
- `research/live_research/orderflow_footprint_latest.json`
- recorded tick/L2 shards under `data/`

The result still says `promotion_verdict=NOT_EVALUATED_AGENT_JOB`. A
`REPLAY_CANDIDATE` row is only a proof task; it does not create a paper lane.

## Audit Files

Defaults:

- `AGENT_GATEWAY_AUDIT_PATH=logs/agent_gateway/audit.jsonl`
- `AGENT_GATEWAY_JOBS_DIR=logs/agent_gateway/jobs`
- `QUANT_OS_AGENT_GATEWAY_DIR=logs/agent_gateway/quant_os`

Each audit record includes `prev_hash` and `hash`, so the gateway call stream
is tamper-evident without mixing it into the order decision journal.

## Next Phases

1. Agent Gateway OpenAPI document and MCP wrapper.
2. Alpha Arena Lite scorer consuming v2 task artifacts.
3. Replay/tick-job adapters for microstructure candidates.
4. UI panels for agent tokens and audit trail.
5. Paper-only agent action scope, after a separate review.
