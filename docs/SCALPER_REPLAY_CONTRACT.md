# Scalper Replay Contract

The formal input/output contract for the microstructure scalper proof path:
the tick/L2 recorder (`vnedge.exchange.tick_recorder`) and the replay
backtester (`vnedge.scalping.replay_backtester`). This is the spec both sides
agree on so recorded data, the replay engine, and any future depth-aware
feature all interoperate without silent drift.

Companion to `docs/SCALPER_GAUNTLET.md` (the research narrative / proof-path
steps). This document is the **contract**; the gauntlet is the **story**.

Status: v1 (top-of-book fill model) + L2 depth recorded but not yet consumed
by the fill model. Deliberately harsh. **Scalping remains unproven — assume
negative until a replay on untouched recorded data says otherwise. No live or
paper exposure.**

---

## 1. Scope

- **DOES prove:** whether a quoting scalper survives a conservative,
  adverse-selection-aware fill model on *real recorded order flow*.
- **Does NOT prove:** anything from candle proxies (already shown dead in
  `SCALPER_GAUNTLET.md`), nor live latency/queue reality beyond the model
  below. A strategy that dies here is dead; one that survives has *a chance*,
  not a guarantee.

## 2. Recorded data schema

Two streams per `(exchange, symbol)`, written by the recorder.

### 2.1 `trades`
| column | type | meaning |
|---|---|---|
| `ts_ms` | int64 | exchange event time, ms since epoch (UTC) |
| `price` | float | trade price |
| `amount` | float | trade size (base) |
| `side` | str | taker side: `"buy"` \| `"sell"` (aggressor) |

### 2.2 `book` (L2)
| column | type | meaning |
|---|---|---|
| `ts_ms` | int64 | book event time, ms since epoch (UTC) |
| `bid`,`bid_qty`,`ask`,`ask_qty` | float | **L1 aliases** == ladder level 0 (kept for the v1 top-of-book engine and legacy L1 data) |
| `bid_px_i`,`bid_qty_i` | float | bid ladder level `i`, `i ∈ [0, levels)` |
| `ask_px_i`,`ask_qty_i` | float | ask ladder level `i`, `i ∈ [0, levels)` |

- `levels` defaults to 10. Missing levels pad `NaN` price / `0.0` qty — the
  schema is **fixed-width** for a given `levels`.
- Invariant: `bid == bid_px_0`, `ask == ask_px_0`, `bid_qty == bid_qty_0`,
  `ask_qty == ask_qty_0`.
- Depth-stream fetch uses `limit=50` (the smallest value **both** Binance
  USDT-M and Bybit swaps accept), sliced to `levels`.

## 3. Storage layout & durability

```
<data_root>/ticks/exchange=<ex>/symbol=<SAFE>/stream=<trades|book>/<YYYYMMDD>/<firstTs>-<seq>.parquet
```
- `SAFE` = symbol with `/` and everything from `:` onward removed
  (`BTC/USDT:USDT` → `BTCUSDT`).
- **Atomic per-flush shards:** each flush writes one new shard via a temp file
  + `os.replace`. Files are never rewritten in place → a concurrent reader
  never observes a partial write; disk cost is O(rows), not O(n²). A crash
  loses at most the un-flushed batch.
- A batch spanning UTC midnight splits into the correct day directories.
- `load_tick_events` reads **both** the shard directory and the legacy single
  `<YYYYMMDD>.parquet` file, so pre-L2 data replays unchanged.

## 4. Event model

- `load_tick_events` merges both streams into one list of `(ts_ms, kind, obj)`,
  `kind ∈ {"book","trade"}`.
- **Ordering:** ascending `ts_ms`. **Tie-break at equal `ts_ms`: `book`
  before `trade`** — the book state a trade executes against must already be
  applied. (Invariant I7.)
- Crossed/invalid book snapshots (`bid ≥ ask`) are dropped (I8).
- Trades with an empty/invalid `side` are dropped (I9).

## 5. Fill model (v1: top-of-book) — the invariants

Entry is a **post-only maker** limit that joins the favored touch; exit is a
**taker** at the opposite touch. One position at a time; no cancel/replace.

- **I1 — trade-through only.** A resting buy fills only when a **sell**-taker
  trade prints **strictly through** the bid (`price < bid`); a resting sell
  only when a **buy**-taker prints `price > ask`. A trade merely *at* the limit
  does **not** fill (that would assume front-of-queue). This is what makes the
  engine capture **adverse selection**: the passive order fills exactly when
  flow pushes against it.
- **I2 — latency guard.** The filling trade must post-date the quote
  (`trade.ts_ms > quote.placed_ms`). A same-instant trade was already in flight
  before the order joined the queue and cannot fill it.
- **I3 — TTL / miss.** An unfilled quote expires at the first event with
  `ts_ms - placed_ms ≥ ttl_ms` and is counted as a **missed fill**, never a
  fill.
- **I4 — no phantom exits through the spread.** A long exits only when the
  **bid** reaches stop/target; a short only when the **ask** does. The engine
  never awards an exit at a price that wasn't actually tradable.
- **I5 — taker exit + slippage.** Exits cross at the tradable touch, then
  `slippage_bps` is applied against the position.
- **I6 — force close at end.** Any position still open at replay end closes at
  the tradable touch (never mid). A quote still resting at end is
  `open_at_end`, **not** a miss (it was never given its full TTL).

## 6. Fee model

`ReplayFees` (bps), applied per round trip: `maker_bps` (entry, default 2.0;
negative = rebate) + `taker_bps` (exit, default 5.0). `slippage_bps`
(default 1.0) applied to the exit price. `net_bps = gross_bps - (maker+taker)`.

## 7. Metrics emitted (`ReplayResult`)

`quotes_placed`, `filled`, `fill_rate`, `missed_fills`, `open_quotes_at_end`,
per-trade `gross_bps` / `fees_bps` / `net_bps` / `adverse_bps` (worst adverse
mid excursion while open, MAE ≤ 0), aggregate `net_usd` on `notional_usd`, and
win rate. `adverse_bps` is the honesty check: negative avg adverse selection on
"winning" strategies is the classic scalper-backtest lie this engine refuses.

## 8. Pass/fail

There is **no auto-pass**. A replay result is a candidate signal, evaluated by
a human through walk-forward on **untouched** recorded windows, exactly like
every other strategy (promotion gates, pre-registered judgment). Positive
`net_usd` on seen data is *not* promotion.

## 9. Conformance tests

Every invariant is locked by a test (`tests/test_replay_backtester.py`,
`tests/test_replay_contract.py`, `tests/test_tick_recorder.py`):

| Invariant | Test |
|---|---|
| I1 trade-through only | `test_conservative_fill_requires_trade_through`, `test_touch_at_limit_does_not_fill`, `test_seller_hitting_bid_fills_us` |
| I2 latency guard | `test_same_instant_trade_does_not_fill` |
| I3 TTL / miss | `test_trade_at_exact_ttl_boundary_does_not_fill`, `test_missed_fill_on_ttl_expiry` |
| I4 no phantom exits | `test_long_target_requires_bid_through_target_not_just_ask`, `test_short_target_requires_ask_through_target_not_just_bid` |
| I5/I6 taker exit / end close | `test_stop_exit_is_a_loss`, `test_end_close_uses_tradable_bid_not_mid_for_long`, `test_quote_censored_at_replay_end_is_not_missed` |
| I7 book-before-trade tie-break | `test_book_precedes_trade_at_equal_timestamp` |
| I8 crossed book dropped | `test_crossed_book_snapshot_skipped` |
| I9 invalid trade side dropped | `test_loader_skips_invalid_trade_side` |
| L2 schema / L1 aliases | `test_book_row_captures_full_ladder_with_l1_aliases`, `test_replay_contract_book_schema` |
| atomic sharded writes | `test_buffer_writes_atomic_shards_never_rewrites` |

## 10. v1 limitations → Phase 2B

Recorded now, **not yet consumed** by the fill model (2B work):
- **Queue position / maker-fill probability** from L2 depth ahead of the touch
  (v1 fills on trade-through regardless of queue size).
- **Liquidity-aware slippage** (walking the book) for larger clips.
- **Multi-level book imbalance** features.
- **Fee-wall metric** + **per-symbol spread/liquidity ranking** across recorded
  symbols — "which pairs can actually clear fees."
