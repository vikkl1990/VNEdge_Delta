# VNEDGE design spec — adopted requirements for upcoming milestones

Consolidated from the 2026-07-02 architecture reviews. These are the
implementation contracts for milestones 2–6. Anything infrastructure-flavored
from those reviews (UDS daemons, NATS, per-exchange processes) was rejected
for v1 — see CLAUDE.md "Architecture decisions".

## 1. Market data quality gate (milestone 2)

Sits between every exchange feed and everything else. Data that fails a check
never reaches stores, features, or strategies; failures are counted and
alerted, and sustained failure marks the feed unhealthy (which the risk
gateway already rejects on via `exchange_healthy` / `data_freshness`).

Checks, in order:

| Check | Rejects |
|---|---|
| Sequence-gap detection | L2 deltas with missing sequence numbers → full book resync |
| Checksum validation | Corrupt books (Bybit/OKX provide checksums; Binance uses update IDs) |
| Staleness guard | Events older than `max_data_staleness_seconds` |
| Clock-skew monitor | Local vs exchange timestamp drift beyond threshold |
| Spread/depth sanity | Crossed books, empty sides, absurd spreads |
| Mark/index divergence | Mark price far from index → liquidation distortion risk |
| Private-stream freshness | Stale position/order/balance stream → state not trusted |
| Reconnect resync | After any disconnect: full snapshot rebuild before deltas resume |

## 2. Order state machine (milestone 5)

```
SIGNAL_CREATED → RISK_REQUESTED → { RISK_REJECTED | RISK_APPROVED }
RISK_APPROVED → ORDER_INTENT_CREATED → SUBMITTING → ACKNOWLEDGED
ACKNOWLEDGED → { PARTIALLY_FILLED → FILLED | FILLED | CANCEL_REQUESTED → CANCELLED | REJECTED }
SUBMITTING → TIMEOUT_UNKNOWN → RECONCILING → (resolved state)
```

Rules:

- **Idempotency key is minted once at ORDER_INTENT_CREATED**, written to the
  decision journal before submission, and reused verbatim on every retry.
  Never derived from timestamps.
- **TIMEOUT_UNKNOWN is the hardest live failure**: submission sent, no ack.
  While ANY order is in TIMEOUT_UNKNOWN or RECONCILING, the account blocks all
  new risk-increasing orders; reduce-only remains available. Resolution only
  via exchange reconciliation, never by assumption.
- Duplicate-intent registry: same intent key seen twice → second is dropped
  and logged loudly.
- Every submission classifies exchange errors: retryable (rate limit,
  timeout) vs terminal (insufficient margin, invalid symbol) vs
  unknown-state-inducing. Retries use bounded backoff and respect venue rate
  limits.
- Emergency flatten: cancel all working orders, close all positions
  reduce-only, in one idempotent operation. Validated with the bounded
  production mainnet execution drill before any live enablement; testnet data
  and fills are not accepted as scalper execution evidence.

## 3. Exchange reconciliation engine (milestone 6)

Inputs: private WS stream + periodic REST snapshots + internal state.
Compares: positions, open orders, balances, fills, fees, funding payments,
**margin mode, and leverage setting** (drift in the last two silently changes
liquidation math).

Current implementation note: `vnedge.execution.private_stream` consumes
CCXT-Pro private order/fill events, normalizes venue statuses/trades, dedupes
fills by trade id, and applies them through `OrderManager` so every update is
state-machine checked and journaled. This is the real-time order/fill input;
positions, balances, margin mode, and leverage drift still require the
periodic REST reconciliation path before live activation.

Fail-closed rule on any mismatch:
1. Stop opening new positions (risk gateway flag).
2. Enter reduce-only mode.
3. Rebuild internal state from exchange truth.
4. Alert operator.
5. Resume entries only after a clean reconciliation pass.

## 4. Decision journal / WAL (milestone 5)

Local append-only JSONL, written **before** any order submission: signal,
features hash, risk decision (all failed/passed checks), intent + idempotency
key, submission, ack, fills, errors. This is the recovery baseline after a
crash and the source for the audit ledger.

Journal-unavailable rule: if the journal cannot be written (disk full,
permission), no new risk-increasing orders; reduce-only exits still allowed.

## 5. Risk config overrides (v2)

Current `RiskConfig` is global. v2 adds layered overrides resolved as
per-symbol > per-exchange > per-mode > global, all validated by the same
pydantic model so no override can exceed global hard caps.

## 6. Monitoring dashboard (milestone 7)

Read-only, out of the execution path, boring on purpose. No NATS, no bridge
daemons — a small FastAPI app.

- **Data model: coalesced state snapshots, not event streams.** One snapshot
  object (mode, equity, daily PnL, drawdown, open positions, working orders,
  feed health, kill-switch state, last reconciliation result) pushed over a
  WebSocket at ~1Hz and served at GET /state. Snapshots are complete, so
  reconnects need no replay and bursts can never firehose the browser.
- **Security:** binds to 127.0.0.1 only (VPS access via SSH tunnel); bearer
  token even on localhost. v1 exposes ZERO control actions. When a kill-switch
  button is added (v2), it is a separate authenticated endpoint that requires
  a confirmation phrase and writes to the audit journal — same rigor as any
  order path.
- **Isolation:** the snapshot is built by the bot's medium loop and handed to
  the UI server as an immutable object; a slow or dead browser can never
  block trading. Failed WebSocket sends deregister the client immediately.
- Frontend: single static HTML page, vanilla JS, auto-reconnect with backoff.
  No build step. Telegram alerts (already planned) remain the primary
  operator channel; the dashboard is for inspection, not operation.
- Config (env): `DASHBOARD_HOST=127.0.0.1`, `DASHBOARD_PORT=8080`,
  `DASHBOARD_TOKEN` (back-compat shared token) and/or `DASHBOARD_USERS`
  (per-user tokens with roles + expiry — see docs/DASHBOARD_AUTH.md; at
  least one user required — no token, no dashboard),
  `DASHBOARD_SNAPSHOT_HZ=1`.
- Snapshot DTO fields: ts, mode, live_trading_enabled, kill_switch_active,
  equity, realized_pnl, unrealized_pnl, daily_loss, consecutive_losses,
  risk_status, feed_health {exchange, candles, funding, open_interest,
  last_update_ms}, positions[], open_orders[], last_risk_reject,
  last_journal_write. Every message self-contained.
- Hard invariants: dashboard failure never affects strategy/risk/orders/
  journal/reconciliation; cannot place orders, change risk config, unlock
  live trading, or disable the kill switch; degrades by dropping UI updates,
  never by slowing the bot; safe to close at any time.

## 7. Explicitly deferred

- Options / Greeks engine, IV surface, expiry risk: **v3**, separate risk
  model, never mixed into the perps path.
- Model registry + canary promotion: v2 (no auto hot-swap ever; human
  approval gate is permanent).
- TimescaleDB historian: v2 (Parquet + SQLite sufficient at v1 volume).
