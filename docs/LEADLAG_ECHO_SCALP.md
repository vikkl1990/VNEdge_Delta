# Cross-Venue Lead-Lag Echo Scalp (research family)

`python -m vnedge.research.leadlag_echo_scalp` — research only. Every guard is
hard-wired: `can_trade=false`, `can_promote=false`, and every published payload
carries the policy block saying so. Nothing in this family places, sizes, or
promotes anything.

This is the tick/L2, queue-aware, maker-fill-proven Phase-2 version of the
lead-lag premise. It is DISTINCT from `event_leadlag_alpha`, which is a cheap
candle-level first pass. Here we work on recorded trades + L2 depth.

## The thesis (architect blueprint, Phase 2)

The deep books — Binance / Bybit — move FIRST. **Delta India ECHOES with a
lag.** When a leader venue impulses, a MAKER-FIRST resting order on Delta's
*follower* book can capture that echo. Three things make this the one scalp
path with a real chance against the fee wall:

1. **Latency is irrelevant.** The information edge is measured in *seconds*
   (the echo lag), not microseconds. Our ~100ms round trip is noise against a
   multi-second edge. We do not race anyone.
2. **We are the sophisticated player on the follower.** On Delta India the
   order flow is thinner and slower; a cross-venue signal that is obvious on
   Binance is not yet priced on Delta.
3. **Maker-first survives the fee wall.** Continuous tick book-imbalance is
   *tombstoned* — after maker/taker/slippage every config was negative
   (`book_imbalance_continuation`, `docs/SCALPER_PARAMETERS.md`). The echo is
   different on two counts: the edge is **directional** (the echo move), not
   spread-capture, AND a maker entry ~halves the round-trip cost. A resting
   order on the follower touch pays the maker fee and gives up the entry
   half-spread instead of crossing it.

The taker-only floor is expected to lose (it pays the full spread twice); the
question this family asks is whether the *maker-first* execution, once proven
by live L2, clears cost.

## Inputs, aligned by recorder wall-clock

- **Leader** — `binanceusdm` trade tape
  (`data/ticks/exchange=binanceusdm/symbol=BTCUSDT/stream=trades`), with the
  `binanceusdm_hist` aggTrades archive as a per-day fallback.
- **Follower** — `delta_india` L2 book
  (`.../exchange=delta_india/symbol=BTCUSD/stream=book`, the ladder the Delta
  native recorder banks since ~2026-07-08) plus its trade tape
  (`stream=trades`) for the queue-aware maker fill.

Symbols are mapped per base asset (`DEFAULT_PAIRS`): BTC leader
`BTC/USDT:USDT` → follower `BTC/USD:USD`; ETH likewise.

> **Honest caveat, stated up front.** Cross-venue timestamp alignment carries
> ~recorder-jitter uncertainty: two independent WS clients, two clocks, two
> network paths. Every lag number here is a **research estimate, not an
> execution guarantee.** And because Delta L2 recording is only days old, the
> leader/follower overlap window is SHORT — the honest answer is very likely
> `UNDER_SAMPLED`, and the family reports that rather than dressing it up.

## Part 1 — causal cross-venue lag estimator

The leader impulse detector (`LeaderImpulseDetector`) is causal by
construction: at each leader trade the signed move is measured against the
OLDEST price still inside the trailing `impulse_window_ms` window — strictly
past data. The current print joins the window only AFTER the decision, and a
`impulse_cooldown_ms` cooldown (from the last fire, also strictly past) stops
one sustained move from firing every tick. Decisions are therefore
**prefix-stable**: truncating the tape after any point cannot change an earlier
decision (unit-tested against every prefix).

For each detected impulse, `estimate_leadlag` measures the lag until the
follower mid first moves `response_threshold_bps` in the impulse direction,
searching only FORWARD and only within `max_lag_ms`. The follower reference is
the last book at or before the impulse (past). The output is a lag distribution
(median / p25 / p75 / mean, response rate) — evidence about *whether and how
fast* Delta echoes, reported alongside the scalp, never used to gate it.

## Part 2 — echo scalp replay (queue-aware maker fill)

One merged, time-ordered pass over three streams (follower book, follower
trades, leader trades — that tie-break order at equal ts keeps the current
follower book fresh before any fill decision). Single position at a time; new
impulses while a scalp is open are counted as overlapping and skipped, never
queued. Per impulse, when flat, one scalp opens with two legs resolved side by
side on the follower book:

- **taker leg** — crosses the follower spread immediately, walked through the
  recorded book (`OrderBookL2.fill_walk`, reused from `depth.py`). Always
  fills. This is the strict floor; the walked VWAP already carries
  liquidity-aware slippage.
- **maker leg** — rests at the follower touch (bid for a long, ask for a
  short). It fills **queue-aware**: the size displayed at that level when we
  joined is our queue; same-side follower taker volume at-or-through our price
  clears that queue first, and we fill only once it is exhausted (the FIFO
  model proven in `TickReplayBacktester(queue_aware=True)`). Unfilled by
  `maker_ttl_ms` ⇒ **missed** (counted, no maker row).

Both legs exit taker (walk the book) at **target** (echo continuation),
**stop** (adverse), or **timeout** (`hold_ms`). Stop is checked before target
on every book update, so **stop wins stop-vs-target ties** — the repo-wide
rule. The echo direction is continuation: an up impulse ⇒ go long the follower,
anticipating the follower rises to catch up.

## Two cost models, side by side

Fees come from the follower venue's registry fee profile
(`delta_india`). Slippage is not modelled in the cost object: taker legs carry
it in the walked VWAP; the maker entry rests at the touch (its whole point) and
pays none.

| model | entry | exit | status |
|---|---|---|---|
| `taker_taker` | taker cross (walked) | taker cross (walked) | honest floor — no fill assumption |
| `maker_first` | maker rest at touch, queue-aware fill | taker cross (walked) | **ASSUMED_QUEUE_FILL** |

The maker-first model's fill is queue-aware against **recorded** L2 depth. That
is stronger than a blind touch-fill, but it is still an assumption: recorded
depth and trade attribution have uncertainty, and a live resting order faces a
real queue we cannot fully reconstruct offline. **No maker-first number can
support candidate status until live L2 validation confirms the fills.** The
payload flags this on the cost model itself (`ASSUMED_QUEUE_FILL`).

## Verdicts

Per pair aggregate across all scanned overlap days
(`min_events_for_candidate` = 20):

- `CANDIDATE` — ≥ 20 events AND positive net under the TAKER floor too
- `MAKER_ONLY_POSITIVE` — ≥ 20 events, taker-negative but maker-first positive;
  blocked on live L2 validation by definition (this is the expected shape if
  the edge is real: the maker fee saving is what carries it)
- `UNDER_SAMPLED` — fewer than 20 events; keep recording, no inference
- `NEGATIVE_EDGE` — ≥ 20 events, negative under both models

## Outputs and folding

- `research/live_research/leadlag_echo_scalp.json` (atomic tmp+replace), via
  the `leadlag-echo-scalp` docker-compose service (6h cadence, read-only data
  mount, log caps).
- `continuous_research` folds the latest payload into `latest.json` under the
  `leadlag_echo_scalp` key — the same read-only pattern as `cascade_reversion`.

## Promotion path (unchanged by this family)

1. **Replay-gated**: a `CANDIDATE`/`MAKER_ONLY_POSITIVE` verdict here is a
   hypothesis. Maker-first evidence additionally requires **live L2
   validation** — resting real (or shadow) orders on Delta and confirming the
   queue-aware fills actually occur — before any maker number counts.
2. **Filtered fresh slice**: re-run on a data slice recorded *after* this code
   existed, to rule out any look-back tuning.
3. **Pre-registered judgment**: declare the exact config and an UNTOUCHED data
   window through the burn registry
   (`vnedge.research.data_burn`, `research/judgments/burn_registry.jsonl`)
   BEFORE looking at the window. One run; the verdict stands.
4. **Shadow**: run as a shadow lane (no capital) to observe live fills and
   cross-venue timing under real conditions.
5. **Human approval**: no paper/shadow/live step without explicit human
   sign-off, and live capital stays behind the full pre-live checklist and the
   three-gate live confirmation regardless.

## Env knobs

| env | default | meaning |
|---|---|---|
| `ECHO_IMPULSE_WINDOW_MS` | 2000 | trailing window for the leader move |
| `ECHO_IMPULSE_THRESHOLD_BPS` | 8.0 | signed move that calls an impulse |
| `ECHO_IMPULSE_COOLDOWN_MS` | 3000 | suppress re-fires after an impulse |
| `ECHO_RESPONSE_THRESHOLD_BPS` | 3.0 | follower move counted as a response |
| `ECHO_MAX_LAG_MS` | 5000 | search horizon for the echo response |
| `ECHO_MAKER_TTL_MS` | 2000 | resting maker order lifetime |
| `ECHO_TARGET_BPS` | 6.0 | echo continuation target |
| `ECHO_STOP_BPS` | 6.0 | adverse stop |
| `ECHO_HOLD_MS` | 10000 | hard timeout after fill |
| `ECHO_NOTIONAL_USD` | 100.0 | clip size walked through the book |
| `ECHO_MIN_EVENTS_FOR_CANDIDATE` | 20 | verdict sample floor |
| `LEADLAG_ECHO_SCALP_INTERVAL_SECONDS` | 21600 | compose service cadence |
