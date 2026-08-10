# Liquidation-Cascade Reversion (research family)

`python -m vnedge.research.cascade_reversion` — research only. Every guard is
hard-wired: `can_trade=false`, `can_promote=false`, and every published
payload carries the policy block saying so. Nothing in this family places,
sizes, or promotes anything.

## Hypothesis

A one-sided liquidation cascade is FORCED flow: positions are closed by the
margin engine, not by anyone's view of value. The flow is therefore
non-informational — it moves price mechanically, and once the cascade
exhausts (no meaningful forced prints for a quiet window), the pressure is
gone. The hypothesis is that price then mean-reverts toward the pre-cascade
reference — the 5-minute pre-cascade trade VWAP — at minute scale.

Structural inputs (both recorded by existing zero-risk collectors):

- **Liquidation stream** — `data/ticks/exchange=<ex>/symbol=<sym>/stream=liquidations`,
  recorded since 2026-07-08 by `vnedge.exchange.liquidation_recorder`
  (binanceusdm forceOrder + bybit allLiquidation, side normalised to the
  FORCED ORDER side on disk: `"sell"` = a long was liquidated).
- **Trade tape** — `stream=trades` from the live tick recorder, with the
  `binanceusdm_hist` aggTrades archive as a per-day fallback for binanceusdm.

## Detection (causal by construction)

All knobs live in `CascadeParams` and are env-tunable (below).

1. **Burst**: rolling 60s (`burst_window_ms`) liquidation-notional sum must
   reach the 95th percentile (`threshold_pct`) of the TRAILING 24h
   (`trailing_window_ms`) rolling sums — computed strictly from PAST events:
   the threshold an event is judged against never includes that event's own
   contribution, so a burst can never raise its own bar, and no future event
   ever leaks in. An absolute floor (`min_burst_notional_usd`) and a warmup
   gate (`min_history_events` past samples) sit under the percentile.
2. **One-sided**: the dominant forced-order side must hold >= 80%
   (`one_sided_min`) of the burst window's notional. Unknown-side rows count
   toward the total but never toward a side — they dilute one-sidedness,
   conservatively.
3. **Exhaustion**: the cascade stays alive while any liquidation prints
   > 25% (`exhaustion_peak_frac`) of the running peak liquidation; the first
   TRADE at least 20s (`exhaustion_quiet_ms`) after the last such print
   confirms exhaustion. A large liquidation inside the quiet window resumes
   the cascade — the replay never enters into a resuming cascade.

## Reversion evaluation (trade tape only)

- **Entry**: the first trade after exhaustion, AGAINST the cascade
  (`"sell"` cascade = longs forced out = price pushed down = buy).
- **Target**: the 5-minute (`pre_vwap_window_ms`) pre-cascade trade VWAP.
  The window ends at the cascade start, so cascade prints never contaminate
  the reference. No pre-cascade trades -> the event is skipped and counted.
- **Stop**: the cascade extreme (most adverse liquidation/trade price during
  the cascade) extended beyond by 10% (`stop_buffer_frac`) of the
  extreme-to-VWAP distance.
- **Timeout**: 15 minutes (`timeout_ms`).
- **Tie-break**: stop is checked before target on every print — stop wins
  ties, the same rule as everywhere else in this repo.
- Degenerate entries (price already at/past the target, or already through
  the stop) are skipped and counted, never traded around.

## Two cost models, side by side

Fees come from the scalper parameter registry's per-exchange fee profiles.

| model | entry | exit | slippage | status |
|---|---|---|---|---|
| `taker_taker` | taker fee | taker fee | both legs, adverse | honest baseline — no fill assumptions |
| `maker_first` | maker fee at the entry print's price | taker fee | exit leg only | **ASSUMED_MAKER_FILL** |

The maker-first model ASSUMES the passive entry filled at the print price.
That is an assumption, not evidence: recorded liquidation cascades move fast,
and a resting limit may never fill or may fill only when adversely selected.
**No maker-first number can support candidate status until L2 queue replay
(`TickReplayBacktester(queue_aware=True)` on recorded book data) confirms the
fills.** The payload flags this on the cost model itself.

## Verdicts

Per exchange x symbol aggregate across all scanned days
(`min_events_for_candidate` = 20):

- `CANDIDATE` — >= 20 events AND positive net under the TAKER model
- `MAKER_ONLY_POSITIVE` — >= 20 events, taker-negative but maker-first
  positive; blocked on L2 queue replay by definition
- `UNDER_SAMPLED` — fewer than 20 events; keep recording, no inference
- `NEGATIVE_EDGE` — >= 20 events, negative under both models

## Outputs and folding

- `research/live_research/cascade_reversion.json` (atomic tmp+replace), via
  the `cascade-reversion` docker-compose service (6h cadence, read-only data
  mount).
- `continuous_research` folds the latest payload into `latest.json` under the
  `cascade_reversion` key — same read-only pattern as `event_taker_replay`.

## Promotion path (unchanged by this family)

1. **Replay-gated**: a `CANDIDATE` verdict here is a hypothesis, nothing
   more. Maker-first evidence additionally requires L2 queue replay first.
2. **Pre-registered judgment**: declare the exact config and an UNTOUCHED
   data window through the burn registry
   (`vnedge.research.data_burn`, `research/judgments/burn_registry.jsonl`)
   BEFORE looking at the window. One run; the verdict stands.
3. **Human approval**: no paper/shadow/live step without explicit human
   sign-off, and live capital stays behind the full pre-live checklist and
   the three-gate live confirmation regardless.

## Env knobs

| env | default | meaning |
|---|---|---|
| `CASCADE_BURST_WINDOW_MS` | 60000 | rolling burst window |
| `CASCADE_TRAILING_WINDOW_MS` | 86400000 | percentile lookback |
| `CASCADE_THRESHOLD_PCT` | 0.95 | percentile of trailing rolling sums |
| `CASCADE_MIN_HISTORY_EVENTS` | 50 | warmup before detection may fire |
| `CASCADE_MIN_BURST_NOTIONAL_USD` | 1000 | absolute burst floor |
| `CASCADE_ONE_SIDED_MIN` | 0.80 | dominant-side share required |
| `CASCADE_EXHAUSTION_PEAK_FRAC` | 0.25 | liq > frac*peak keeps cascade alive |
| `CASCADE_EXHAUSTION_QUIET_MS` | 20000 | quiet time confirming exhaustion |
| `CASCADE_PRE_VWAP_WINDOW_MS` | 300000 | pre-cascade reference window |
| `CASCADE_STOP_BUFFER_FRAC` | 0.10 | stop buffer beyond the extreme |
| `CASCADE_TIMEOUT_MS` | 900000 | hard exit |
| `CASCADE_MIN_EVENTS_FOR_CANDIDATE` | 20 | verdict sample floor |
| `CASCADE_REVERSION_INTERVAL_SECONDS` | 21600 | compose service cadence |
